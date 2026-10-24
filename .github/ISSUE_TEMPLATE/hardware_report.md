---
name: Hardware report
about: You ran Kinoloom on your PC. Report how it went, good or bad.
title: "Hardware report: <GPU>, <RAM>"
labels: hardware-report
---

Kinoloom has been tested on one machine so far (RTX 3070 8 GB, i9-10980XE, 64 GB RAM, Windows 11). Every report like this helps other people know what works.

**Your machine**
- GPU and VRAM:
- NVIDIA driver version:
- CPU:
- System RAM (and how many memory channels, if you know):
- PCIe generation and lanes for the GPU, if you know:
- Windows version:
- Where the model weights live (NVMe SSD, SATA SSD, hard drive):

**What you ran**
- Model or tier (for example `ltx25_s6_cq`, `minimax_h3_hero_v2`, `qwen21_fast`):
- Resolution and length:
- A seamless chain? How many links?

**What you measured**
- Time per clip or per still, cold (first run after start) and warm:
- Peak VRAM, if you looked (`nvidia-smi`):
- GPU temperature and power, if you looked:

**Did anything go wrong?**
Crashes, very slow steps, out-of-memory errors, a frozen render. Logs from `logs\` help a lot.
