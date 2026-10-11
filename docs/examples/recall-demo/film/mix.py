import json, subprocess
TL=json.load(open('timeline.json')); L=TL['lines']; TOTAL=TL['total']
def W(line, word, nth=0):
    k=0
    for t,s in L[line]['words']:
        if s==word or s.startswith(word):
            if k==nth: return t
            k+=1
    raise KeyError(word)
V=lambda l: L[l]['vo']
SFX='sfx/'
today=W('s01','today')
ev=[('type',0.05,-20),('alert',2.0,-16),('type',today+.2,-20),('alert',today+1.8,-16)]
ev+=[('whoosh',V(l)-.35,-24) for l in ['s02','s03','s04','s06','s07','s08','s09','s10','s11','s12']]
ev+=[('type',W('s02','notes')-3.8,-24)]
ev+=[('settle',W('s03','brings'),-16)]
ev+=[('type',W('s04','we'),-20),('click',W('s04','before'),-16),('confirm',W('s04','finds'),-13),
     ('click',W('s05','opens'),-16),('confirm',W('s05','no'),-13)]
ev+=[('settle',W('s06','rule')-.2,-17)]
ev+=[('click',W('s07','hook'),-16),('confirm',W('s07','answer')+.1,-13)]
tr=W('s08','tries'); ev+=[('tick',tr+i*.45,-18) for i in range(4)]
ev+=[('confirm',W('s08','pulls'),-14),('alert',W('s08','when'),-20)]
ev+=[('alert',W('s09','fails'),-15),('click',W('s09','sentinel'),-16),('confirm',W('s09','finds')+.35,-13)]
ev+=[('tick',W('s09',w),-18) for w in ['expired','quotas','disk']]
ev+=[('settle',W('s09','quiet'),-17)]
ev+=[('type',W('s10','write'),-20),('confirm',W('s10','half'),-13),('type',W('s10','next'),-22),('confirm',W('s10','recall')+.45,-14)]
ev+=[('type',W('s11','prove'),-22)]
ev+=[('tick',t,-19) for t in [W('s11','plants')+.2,W('s11','runs'),W('s11','real'),W('s11','against'),W('s11','checks')]]
ev+=[('alert',W('s11','first'),-15)]
ev+=[('tick',W('s11','caught')+.3+i*.32,-21) for i in range(5)]
ev+=[('type',V('s12'),-22),('settle',W('s12','your'),-14),('confirm',W('s12','remember')+.6,-15)]

inputs=[]; filt=[]; labels=[]
# VO
for i,l in enumerate(L):
    inputs+=['-i',f'vo/{l}.mp3']; d=int(V(l)*1000)
    filt.append(f'[{len(inputs)//2-1}:a]aresample=48000,adelay={d}|{d},apad=whole_dur={TOTAL}[v{i}]'); labels.append(f'[v{i}]')
nvo=len(labels)
filt.append(''.join(labels)+f'amix=inputs={nvo}:normalize=0,loudnorm=I=-16:TP=-1.5:LRA=7,asplit=2[vo][vokey]')
# music: extend tail by crossfading the 119-141 s groove after 141 s
inputs+=['-i','music.mp3']; mi=len(inputs)//2-1
filt.append(f'[{mi}:a]aresample=48000,asplit=2[ma][mb]')
filt.append(f'[ma]atrim=0:141.5,asetpts=N/SR/TB[m1]')
filt.append(f'[mb]atrim=117:141.5,asetpts=N/SR/TB[m2]')
filt.append(f'[m1][m2]acrossfade=d=2.5:c1=tri:c2=tri,atrim=0:{TOTAL},afade=t=out:st={TOTAL-4.5}:d=4.5,volume=-15dB[mus]')
filt.append('[mus][vokey]sidechaincompress=threshold=0.04:ratio=6:attack=25:release=450:makeup=1[musd]')
# sfx
sl=[]
for j,(name,t,db) in enumerate(ev):
    inputs+=['-i',SFX+name+'.mp3']; k=len(inputs)//2-1; d=int(max(0,t)*1000)
    filt.append(f'[{k}:a]aresample=48000,volume={db}dB,adelay={d}|{d},apad=whole_dur={TOTAL}[s{j}]'); sl.append(f'[s{j}]')
filt.append(''.join(sl)+f'amix=inputs={len(sl)}:normalize=0[sfx]')
filt.append(f'[vo][musd][sfx]amix=inputs=3:normalize=0,atrim=0:{TOTAL},alimiter=limit=0.89[out]')
cmd=['ffmpeg','-y','-v','error']+inputs+['-filter_complex',';'.join(filt),'-map','[out]','-c:a','pcm_s16le','mix.wav']
subprocess.run(cmd,check=True)
print('events',len(ev),'total',TOTAL)
