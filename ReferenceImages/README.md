[Kinoloom](../README.md) › [Docs](../docs/README.md) › Reference images

# Reference images

> **In short:** This folder can hold face reference pictures for one narrow
> check: the face QA of legacy shots (shots that belong to no project). The
> studio reads it on its own during that check, and nowhere else. Project
> shots, Video Studio and the ComfyUI workflows don't use it.

## What reads this folder

Only one piece of code reads it: `FaceQA.face_similarity` in
`tool/face_qa.py`. The orchestrator runs `FaceQA` on shots without a project
(`_post_and_qa` in `tool/orchestrator.py`), when `automation.auto_post` is
on. For each character in the shot, except the `crowd` tier, it compares the
rendered clip with reference faces.

It looks for reference pictures in this order and uses the first folder that
has any:

1. `ReferenceImages/<id>/`
2. `roster/<id>/`
3. `roster/keyframes/<id>/`
4. `roster/datasets/face/<id>/` (and its `stills/` subfolder)

`<id>` is the character's id as the shot names it. If none of these folders
has a picture, the face check is skipped for that character.

Project shots use `QADirector` (`tool/qa_director.py`) instead, with the
shot's approved keyframe as the reference, so they never read this folder.
Nothing else in `tool/`, `install/`, `tests/` or the `.bat` files names it.
The full face QA rules are in
[PIPELINE_AND_CONTINUITY.md](../docs/PIPELINE_AND_CONTINUITY.md), section 5.1.

## Layout

```text
ReferenceImages/
└── <id>/
    ├── front.png
    └── side.jpg
```

- The folder name must be the character id exactly.
- Put the pictures directly in `ReferenceImages/<id>/`. Subfolders are not
  searched.
- Only `.jpg` and `.png` files are read.

## Git

`.gitignore` ignores everything in this folder except this README, so the
pictures you put here stay out of git. Forcing one in (`git add -f`) would
publish it with your next push; keep private or licensed pictures out of your
commits.
