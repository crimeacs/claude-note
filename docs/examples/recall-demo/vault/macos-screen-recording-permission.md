---
title: "macOS screen recording permission attaches to the built binary"
type: gotcha
created: 2026-07-14
assistant: claude-code
tags: [macos, permissions]
---
# macOS screen recording permission attaches to the built binary

ScreenCaptureKit fails with OSStatus -3801 until a person enables Screen Recording in System
Settings for the exact binary that runs. Every rebuild produces a new binary that must be granted
again. There is no programmatic fix; stop and tell the user which toggle to flip.
