---
name: Bug report
about: Something broke, crashed, or did something it shouldn't
title: ""
labels: bug
---

**What happened**
A short description of the problem.

**What you expected**

**How to make it happen again**
1.
2.
3.

**Your setup**
- Windows version:
- GPU and VRAM:
- NVIDIA driver version (`nvidia-smi`):
- System RAM:
- Which page or command (Video Studio, Render, `start.bat`, a script...):
- Which model or tier, if it was a render (for example `ltx25_s6_cq`):

**Logs**
If the install failed, attach `logs\install\install.log`, and paste `logs\install\LAST-FAILURE.txt` if it is there. The `pip-*.log` files next to them have pip's detailed log, if you are asked for it. These logs show your folder paths, which often include your Windows user name.

For a problem after the install, the launcher writes one log per component to `logs\` (for example `logs\ui.log`, `logs\comfyui_8188.log`, `logs\qwen_8190.log`). Paste the part around the error. Please remove tokens from anything you paste: `tool\settings.yaml` holds your API and fleet tokens.

If this is a security problem, please don't open a public issue. Follow `SECURITY.md` in the repository instead.
