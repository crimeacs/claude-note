use std::io::{Read, Write};
use std::net::TcpStream;
use std::time::Duration;

fn main() {
    let port = std::env::var("PORT").unwrap_or_else(|_| "3007".into());
    let mut stream = TcpStream::connect(format!("127.0.0.1:{port}"))
        .unwrap_or_else(|_| std::process::exit(1));
    stream.set_read_timeout(Some(Duration::from_secs(3))).ok();
    let _ = stream.write_all(b"GET /health HTTP/1.0\r\nHost: localhost\r\n\r\n");
    let mut buf = [0u8; 32];
    if let Ok(n) = stream.read(&mut buf) {
        if buf[..n].windows(6).any(|w| w == b"200 OK") {
            std::process::exit(0);
        }
    }
    std::process::exit(1);
}
