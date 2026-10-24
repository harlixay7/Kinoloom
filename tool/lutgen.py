"""Generate the global cinematic LUT (teal shadows / warm highlights).
Writes a 3D LUT .cube used by the post grade pass."""
import numpy as np

from core import ROOT

OUT = ROOT / "bible" / "luts" / "cinematic_teal_amber.cube"


def main() -> None:
    N = 33
    OUT.parent.mkdir(parents=True, exist_ok=True)
    # teal shadows, neutral mids, amber highlights
    teal = np.array([0.55, 0.72, 0.85])
    warm = np.array([1.10, 1.02, 0.88])
    sat = 1.08

    def lookup(r, g, b):
        t = (r + g + b) / 3.0
        color = np.array([r, g, b])
        mix = np.clip((t - 0.5) * 2.0 + 0.5, 0.0, 1.0)  # 0 = shadows, 1 = highlights
        target = teal * (1 - mix) + warm * mix
        color = color * target
        gray = np.array([color.mean()] * 3)
        color = gray + (color - gray) * sat
        return np.clip(color, 0, 1)

    with open(OUT, "w") as f:
        f.write("TITLE \"cinematic_teal_amber\"\n")
        f.write(f"LUT_3D_SIZE {N}\n")
        f.write("DOMAIN_MIN 0.0 0.0 0.0\n")
        f.write("DOMAIN_MAX 1.0 1.0 1.0\n")
        for b in range(N):
            for g in range(N):
                for r in range(N):
                    rout, gout, bout = lookup(r / (N - 1), g / (N - 1), b / (N - 1))
                    f.write(f"{rout:.6f} {gout:.6f} {bout:.6f}\n")
    print("LUT written:", OUT)


if __name__ == "__main__":
    main()
