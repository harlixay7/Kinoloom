"""post.PostPipeline._apply_facefix: every-n-th face-fix carried onto the
frames in between without freezing their motion."""
import cv2
import numpy as np

from post import PostPipeline


def _frames(tmp_path, n=7):
    paths = []
    for i in range(n):
        img = np.full((64, 64, 3), 40 + i * 10, np.uint8)   # each frame differs
        p = tmp_path / f"f_{i + 1:05d}.png"
        cv2.imwrite(str(p), img)
        paths.append(str(p))
    return paths


def test_processed_and_between_frames_get_the_fix(tmp_path):
    all_frames = _frames(tmp_path)
    work = all_frames[::3]                       # 0, 3, 6
    out = tmp_path / "out"
    out.mkdir()
    done = []
    for i, wf in enumerate(work):
        fixed = cv2.imread(wf).astype(np.int16)
        fixed[16:48, 16:48] += 60                 # the "face" region
        p = out / f"f_{i + 1:05d}.png"
        cv2.imwrite(str(p), np.clip(fixed, 0, 255).astype(np.uint8))
        done.append(str(p))
    before = [cv2.imread(f).astype(np.int16) for f in all_frames]

    PostPipeline._apply_facefix(all_frames, work, done)

    for i, f in enumerate(all_frames):
        img = cv2.imread(f).astype(np.int16)
        # background (outside the feathered mask) keeps the frame's own pixels
        assert abs(int(img[2, 2, 0]) - int(before[i][2, 2, 0])) <= 1
        # face centre carries the correction on EVERY frame — no flicker
        assert int(img[32, 32, 0]) - int(before[i][32, 32, 0]) >= 55
    # frames 1 and 2 kept their own (different) base values: no freeze
    assert int(cv2.imread(all_frames[1])[2, 2, 0]) != int(cv2.imread(all_frames[3])[2, 2, 0])


def test_size_mismatch_takes_fixed_frame(tmp_path):
    all_frames = _frames(tmp_path, 2)
    big = tmp_path / "big.png"
    cv2.imwrite(str(big), np.zeros((128, 128, 3), np.uint8))
    PostPipeline._apply_facefix(all_frames, all_frames[:1], [str(big)])
    assert cv2.imread(all_frames[0]).shape == (128, 128, 3)
