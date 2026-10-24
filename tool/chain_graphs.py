"""API-format graphs for seamless chain continuations (LTX-2.5 and MiniMax H3).

A chain renders one long take as links. Every pass that touches pixels the viewer
has already seen keeps them under noise mask 0, so a later pass continues those
frames instead of redrawing them:

LTX-2.5 (per link, two passes, per-stage carries)
    draft  (stage 1, 960x544): the previous link's last 25 stage-1 frames are
           pinned in place, its audio over those frames is pinned in the audio
           stream, the MSR guide carries the ORIGINAL reference set of link 1.
           Saves the stage-1 video/keyframe/audio latents, a lossless preview
           and the tail frames for the next link's draft.
    finish (stage 2, 1920x1088): the stage-1 latent is upsampled, the previous
           link's FINAL last 25 frames are re-pinned at full resolution (the
           official I2V template's "re-apply I2V on the x2 canvas"), the stage-1
           audio is frozen as cross-attention context, and the draft's own model
           (MSR IC-LoRA, CQ if the draft had it) refines from sigma 0.55 with
           euler_ancestral. The
           official distilled schedule (0.909375 + euler, bare model) re-drew the
           wardrobe and faces of a reference-guided draft (lab 2026-10-23).
MiniMax H3 (per link, then once per take)
    link   (base, 960x544): link 1 opens on the start image (frame-0 guide); later
           links get the previous link's last 39 frames + 65 audio ticks copied in
           as an exact latent prefix (no VAE round trip, no still-image anchor).
    refine (once, 1920x1088): the links are joined into one take latent, upscaled
           with the learned 3D upscaler, refined by MMH3SplitUpscale in temporal
           chunks that pin the previous chunk's refined overlap and never put a
           chunk junction on a chain seam, each chunk conditioned on its own
           link's prompt (encoded beforehand in a graph of its own and loaded
           from disk, so the refine's server never holds the text encoder);
           saved as one latent and decoded once in a graph of its own (no DiT
           next to the 1080p frames).

Graphs reference files under ComfyUI's output directory through relative paths
("studio_chain/<chain id>/..."), handled by the ComfyUI-LTX25-Studio nodes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

LTX_S6_SIGMAS = "1.0, 0.9875, 0.975, 0.909375, 0.725, 0.421875, 0.0"
# Stage 2 refines the upsampled draft from well below the official 0.909375, which
# leaves 9% of it: with no references in stage 2 (they do not fit at 1080p) the model
# re-invents what the prompt does not name. Gate link 1, 2026-10-23, vs the draft:
# from 0.725, SSIM 0.55 -> 0.72 and mid-band texture corr 0.35 -> 0.71, the cream
# cable knit kept instead of turned floral. From 0.725 it still re-drew the faces
# older (forehead furrows; the lead's apparent age 29.5 in the draft -> 33.1, ArcFace
# similarity to her portrait 0.394 -> 0.241); from 0.55 her age stays 29.5 (0.351),
# SSIM 0.82, texture 0.83, with the same 1080p detail and the same two steps.
LTX_STAGE2_SIGMAS = "0.55, 0.3, 0.0"
H3_REFINE_SIGMAS = "0.4500, 0.3612, 0.3032, 0.1967, 0.1081, 0.0000"

LTX_CARRY_FRAMES = 25       # 1 + 3*8: four latent frames of motion context
H3_CARRY_FRAMES = 39        # 17*2 + 5: twelve tokens, 65 audio ticks exactly
H3_TOKEN_PERIOD = 5         # an H3 token group: 1 + 4*4 frames (studio_chain_core.H3_FRAME_PER_TOKEN)


def h3_tokens(frames: int) -> int:
    """Video tokens of an H3 clip of `frames` (17k+5) frames."""
    return (int(frames) - 5) // 17 * H3_TOKEN_PERIOD + 2


@dataclass
class LtxModels:
    unet: str = "ltx-2.5-22b-distilled-transformer-w4a8_convrot.safetensors"
    clip: str = "gemma4-12b-with-proj-ltx-2.5-w4a8_convrot.safetensors"
    vae: str = "ltx-2.5-video-vae-conv-bf16.safetensors"
    audio_vae: str = "ltx-2.5-audio-vae-bf16.safetensors"
    upscaler: str = "ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors"
    msr_lora: str = "LTX-2.5-Licon-MSR-V1.safetensors"
    cq_lora: str = "ltx2.5-CQ-enhancer-lora-for-videos-rank128.safetensors"

    @classmethod
    def from_registry(cls, entry: dict) -> "LtxModels":
        m = cls()
        if entry.get("path"):
            m.unet = Path(entry["path"]).name
        if entry.get("text_encoder"):
            m.clip = Path(entry["text_encoder"]).name
        if entry.get("vae"):
            m.vae = Path(entry["vae"]).name
        return m


@dataclass
class H3Models:
    unet: str = "Minimax-h3_Singularity_ref2va_v1.3_Pruned_w4a8.safetensors"
    clip: str = "qwen3vl_32b_minimax_h3-w4a8_convrot.safetensors"
    vae: str = "minimax_h3_video_vae_fp16.safetensors"
    audio_vae: str = "minimax_h3_audio_vae_fp32.safetensors"
    upscaler: str = "minimax_h3_latent_upscaler_3d_conv_v1_bf16.safetensors"

    @classmethod
    def from_registry(cls, entry: dict) -> "H3Models":
        m = cls()
        for attr, key in (("unet", "path"), ("clip", "text_encoder"), ("vae", "vae"),
                          ("audio_vae", "vae_audio"), ("upscaler", "latent_upscaler")):
            if entry.get(key):
                setattr(m, attr, Path(entry[key]).name)
        return m


class _G:
    """Tiny API-graph builder: add() returns the node id; ref(id, slot) links."""

    def __init__(self) -> None:
        self.nodes: dict[str, dict] = {}
        self._n = 0

    def add(self, class_type: str, /, **inputs) -> str:
        self._n += 1
        nid = str(self._n)
        self.nodes[nid] = {"class_type": class_type, "inputs": inputs}
        return nid


def ref(nid: str, slot: int = 0) -> list:
    return [nid, slot]


# ======================================================================== LTX
@dataclass
class LtxCarry:
    tail_frames: str          # StudioSaveImages relpath of the previous link's last frames
    audio: str                # StudioSaveLatent relpath of the previous link's stage-1 audio
    source_frames: int        # pixel frames of the previous link
    frames: int = LTX_CARRY_FRAMES
    latent: str | None = None  # previous link's saved video latent: carry it exactly (no re-encode)


@dataclass
class LtxDraft:
    prompt: str
    seed: int
    frames: int
    out: str                                  # relpath prefix for this link's files
    refs: dict[str, str] = field(default_factory=dict)   # slot -> ComfyUI input image name
    negative: str = ""
    width: int = 960
    height: int = 544
    fps: float = 24.0
    carry: LtxCarry | None = None
    cq_strength: float = 0.6
    msr_strength: float = 1.0
    sigmas: str = LTX_S6_SIGMAS
    sampler: str = "euler_ancestral"
    keyframe_interval: int = 24
    tail_out: int = LTX_CARRY_FRAMES
    pin_strengths: str = ""                   # per carried latent, "" = hard pin (1.0)
    carry_restore: float = 0.0                # undo the soft-pin gain on the carry (0 = off)
    seam_adain: float = 0.0                   # look match across the carry end (0 = off)
    seam_ramp: int = 6                        # latents the match fades out over
    seam_mode: str = "window"                 # StudioSeamAdain mode ("window" | "path")
    release_text_encoder: bool = True         # unload the encoder before the sampler (RAM)
    models: LtxModels = field(default_factory=LtxModels)


def _carry_latents(carry_frames: int) -> int:
    return (int(carry_frames) - 1) // 8 + 1


def _seam_mode(mode: str) -> dict:
    """StudioSeamAdain's optional mode input; omitted for the node default ("window"),
    so graphs built before the mode existed keep their digest."""
    if mode not in ("window", "path"):
        raise ValueError(f"seam mode {mode!r} must be 'window' or 'path'")
    return {} if mode == "window" else {"mode": mode}


@dataclass
class LtxFinish:
    prompt: str
    seed: int
    frames: int
    draft: str                                # relpath prefix the draft saved under
    out: str
    negative: str = ""
    fps: float = 24.0
    carry_tail: str | None = None             # previous link's FINAL tail frames relpath
    carry_latent: str | None = None           # previous link's FINAL video latent (exact carry)
    carry_frames: int = LTX_CARRY_FRAMES
    cq_strength: float = 0.6                  # the draft's model (the chain passes the draft's value)
    drift_reference: str | None = None        # link 1's stage-1 video latent relpath
    drift_factor: float = 0.0
    drift_ramp: int = 8
    sigmas: str = LTX_STAGE2_SIGMAS
    sampler: str = "euler_ancestral"
    keyframe_interval: int = 24
    tail_out: int = LTX_CARRY_FRAMES
    decode_tile: int = 512
    # The MSR README's two-stage recipe re-applies the references at full resolution.
    # Off by default: at 1920x1088 the 15 reference latent frames take the stage-2
    # token count from ~43k to ~73k, the 11.9 GB model no longer stays resident on
    # an 8 GB card, and in the one run that tried it (2026-10-22/23, gate F0 link 1)
    # the first step had not finished after 7 min 37 s. The finish then runs the
    # draft's model without the guide.
    refs: dict[str, str] = field(default_factory=dict)
    pin_strengths: str = ""                   # per carried latent, "" = hard pin (1.0)
    carry_restore: float = 0.0
    seam_adain: float = 0.0
    seam_ramp: int = 6
    seam_mode: str = "window"
    msr_stage2: bool = False
    msr_strength: float = 1.0
    # With references, load the MSR IC-LoRA even without the guide: stage 2 then
    # refines with the model that drew the draft (as the single-shot workflows do).
    msr_lora: bool = True
    reference_frames: str = "33"
    release_text_encoder: bool = True
    # Warm-start the stage-1 detailing keyframes as guides. Off: +5 latent frames =
    # +10.2k tokens at 1920x1088 put link 1 at 733 s instead of 287 s, and a 145-frame
    # link with them (~49k tokens) stalled until the lab watchdog stopped it (the
    # 1080p step cost is steeply superlinear in tokens on this card); they did not
    # keep the wardrobe either (gate link 1, 2026-10-23).
    stage2_keyframes: bool = False
    models: LtxModels = field(default_factory=LtxModels)


def _ltx_text(g: _G, m: LtxModels, prompt: str, negative: str, fps: float) -> tuple[str, str]:
    clip = g.add("CLIPLoader", clip_name=m.clip, type="ltxv", device="default")
    pos = g.add("CLIPTextEncode", text=prompt, clip=ref(clip))
    neg = g.add("CLIPTextEncode", text=negative, clip=ref(clip))
    cond = g.add("LTXVConditioning", positive=ref(pos), negative=ref(neg), frame_rate=float(fps))
    return cond, clip


def _release_text_encoder(g: _G, value: list, encoder_only: bool = False) -> list:
    """Pass `value` (produced after the last text encode) through an eviction that
    returns the text encoder's VRAM and pinned RAM before the DiT samples
    (unload_all_models). Without it the encoder (10 GB LTX / 15 GB H3) stays pinned
    through the sampler next to the DiT: on gate v2's link-2 1080p finish
    (2026-10-23, ~49k tokens with its keyframes) the server's RSS reached 39 GB and
    available RAM fell to 0.55 GB. The rerun with the release held 13-15 GB less RSS
    but stalled as well, because that finish was past the 1080p token cliff. Costs a
    re-stage of the encoder on the next prompt.

    encoder_only keeps the DiT and VAEs staged: the full eviction, with the lab's
    /free between jobs, made the H3 smoke links re-stage the 11 GB DiT before their
    first step (92.7 s against ~12 s for the later steps; 2026-10-23). LTX graphs drop the DiT before every decode anyway (#15606), and
    the full release also clears the VAE before a 1080p finish that runs at the VRAM
    cliff, so they keep it. H3 chain graphs no longer release at all
    (H3Link.release_text_encoder, see there); the H3 refine samples in a server
    without the encoder instead (h3_refine_cond_graph)."""
    extra = {"text_encoder_only": True} if encoder_only else {}
    return ref(g.add("LTX25EvictModels", value=value, enabled=True, keep_text_encoder=False, **extra))


def ltx_draft_graph(p: LtxDraft) -> dict:
    """Without references (no pic1) the link renders text-to-video plus its carries."""
    if p.refs and "pic1" not in p.refs:
        raise ValueError("the MSR guide needs a pic1 reference (slots fill from pic1)")
    use_msr = bool(p.refs)
    g, m = _G(), p.models
    unet = g.add("UNETLoader", unet_name=m.unet, weight_dtype="default")
    model = unet
    if p.cq_strength > 0:
        model = g.add("LoraLoaderModelOnly", model=ref(unet), lora_name=m.cq_lora,
                      strength_model=float(p.cq_strength))
    msr: str | None = None
    if use_msr:
        model = msr = g.add("ComfyUILTX25MSRICLoRALoader", model=ref(model), lora_name=m.msr_lora,
                            strength_model=float(p.msr_strength))
    cond, _clip = _ltx_text(g, m, p.prompt, p.negative, p.fps)
    pos, neg = ref(cond, 0), ref(cond, 1)
    if p.release_text_encoder:
        pos = _release_text_encoder(g, pos)
    vae = g.add("VAELoader", vae_name=m.vae)
    avae = g.add("LTXVAudioVAELoader", ckpt_name=m.audio_vae)
    latent = g.add("EmptyLTXVLatentVideo", width=p.width, height=p.height,
                   length=int(p.frames), batch_size=1)
    pinned = None                    # the latent holding the encoded carry
    if p.carry:
        tail = g.add("StudioLoadImages", relpath=p.carry.tail_frames)
        if p.carry.latent:
            src = g.add("StudioLoadLatent", relpath=p.carry.latent)
            latent = pinned = g.add("StudioLtxLatentCarry", vae=ref(vae), image=ref(tail), latent=ref(latent),
                                    source=ref(src), carry_latents=_carry_latents(p.carry.frames), strength=1.0)
        else:
            latent = pinned = g.add("LTXVImgToVideoInplace", vae=ref(vae), image=ref(tail),
                                    latent=ref(latent), strength=1.0, bypass=False)
        if p.pin_strengths:
            latent = g.add("StudioPinRamp", latent=ref(latent), strengths=p.pin_strengths)
    kf = g.add("LTXVAddGeneratedKeyframes", positive=pos, negative=neg,
               vae=ref(vae), latent=ref(latent), interval_frames=int(p.keyframe_interval))
    guide = kf                       # (positive, negative, latent) the sampler sees
    if use_msr and msr is not None:
        guide_inputs = {slot: ref(g.add("LoadImage", image=p.refs[slot]))
                        for slot in ("pic1", "pic2", "pic3", "pic4", "background") if p.refs.get(slot)}
        guide = g.add("ComfyUILTX25MSRMultiReferenceGuide", positive=ref(kf, 0), negative=ref(kf, 1),
                      vae=ref(vae), latent=ref(kf, 2), strength=1.0, reference_frames="33",
                      use_tiled_encode=False, tile_size=256, tile_overlap=64,
                      msr_parameters=ref(msr, 1), **guide_inputs)
    av_inputs = dict(video_latent=ref(guide, 2), frame_rate=float(p.fps),
                     audio_latents_per_second=25.0, frames=int(p.frames))
    if p.carry:
        src_audio = g.add("StudioLoadLatent", relpath=p.carry.audio)
        av_inputs.update(source_audio=ref(src_audio), source_frames=int(p.carry.source_frames),
                         carry_frames=int(p.carry.frames))
    av = g.add("LTX25CarryAudioLatent", **av_inputs)
    sigmas = g.add("ManualSigmas", sigmas=p.sigmas)
    sampler = g.add("KSamplerSelect", sampler_name=p.sampler)
    noise = g.add("RandomNoise", noise_seed=int(p.seed))
    guider = g.add("LTXVDualCFGGuider", model=ref(model), positive=ref(guide, 0),
                   negative=ref(guide, 1), video_cfg=1.0, audio_cfg=1.0)
    sampled = g.add("SamplerCustomAdvanced", noise=ref(noise), guider=ref(guider),
                    sampler=ref(sampler), sigmas=ref(sigmas), latent_image=ref(av))
    sep = g.add("LTXVSeparateAVLatent", av_latent=ref(sampled))
    # peel the detailing keyframes first, then drop the MSR guide frames
    peel = g.add("LTXVSeparateGeneratedKeyframes", positive=ref(guide, 0), negative=ref(guide, 1),
                 latent=ref(sep, 0), keyframes_to_batch=False)
    video = ref(peel, 2)
    if use_msr:
        video = ref(g.add("LTXVCropGuides", positive=ref(peel, 0), negative=ref(peel, 1), latent=ref(peel, 2)), 2)
    if p.carry and pinned and p.pin_strengths and p.carry_restore > 0:
        video = ref(g.add("StudioCarryRestore", latent=video, reference=ref(pinned),
                          carry_latents=_carry_latents(p.carry.frames), factor=float(p.carry_restore)), 0)
    if p.carry and p.seam_adain > 0:
        video = ref(g.add("StudioSeamAdain", latent=video, carry_latents=_carry_latents(p.carry.frames),
                          ramp_latents=int(p.seam_ramp), factor=float(p.seam_adain), **_seam_mode(p.seam_mode)))
    g.add("StudioSaveLatent", latent=video, relpath=f"{p.out}_s1_video", save_noise_mask=False)
    g.add("StudioSaveLatent", latent=ref(peel, 3), relpath=f"{p.out}_s1_kf", save_noise_mask=False)
    g.add("StudioSaveLatent", latent=ref(sep, 1), relpath=f"{p.out}_s1_audio", save_noise_mask=False)
    evict = g.add("LTX25EvictModels", value=video, enabled=True, keep_text_encoder=True)
    frames = g.add("VAEDecodeTiled", samples=ref(evict), vae=ref(vae), tile_size=512, overlap=64,
                   temporal_size=64, temporal_overlap=16)
    audio = g.add("LTXVAudioVAEDecode", samples=ref(sep, 1), audio_vae=ref(avae))
    g.add("StudioSaveVideoLossless", images=ref(frames), fps=float(p.fps), relpath=f"{p.out}_draft",
          audio=ref(audio))
    g.add("StudioSaveImages", images=ref(frames), relpath=f"{p.out}_draft_tail", last_n=int(p.tail_out))
    return g.nodes


def ltx_finish_graph(p: LtxFinish) -> dict:
    if p.refs and "pic1" not in p.refs:
        raise ValueError("the MSR guide needs a pic1 reference (slots fill from pic1)")
    use_msr = bool(p.msr_stage2 and p.refs)
    g, m = _G(), p.models
    model = g.add("UNETLoader", unet_name=m.unet, weight_dtype="default")
    if p.cq_strength > 0:
        model = g.add("LoraLoaderModelOnly", model=ref(model), lora_name=m.cq_lora,
                      strength_model=float(p.cq_strength))
    msr: str | None = None
    if use_msr or (p.msr_lora and p.refs):
        model = msr = g.add("ComfyUILTX25MSRICLoRALoader", model=ref(model), lora_name=m.msr_lora,
                            strength_model=float(p.msr_strength))
    cond, _clip = _ltx_text(g, m, p.prompt, p.negative, p.fps)
    pos, neg = ref(cond, 0), ref(cond, 1)
    if p.release_text_encoder:
        pos = _release_text_encoder(g, pos)
    vae = g.add("VAELoader", vae_name=m.vae)
    avae = g.add("LTXVAudioVAELoader", ckpt_name=m.audio_vae)
    ups = g.add("LatentUpscaleModelLoader", model_name=m.upscaler)
    s1 = g.add("StudioLoadLatent", relpath=f"{p.draft}_s1_video")
    if p.drift_reference and p.drift_factor > 0:
        ref_lat = g.add("StudioLoadLatent", relpath=p.drift_reference)
        skip = ((p.carry_frames - 1) // 8 + 1) if p.carry_tail else 0
        s1 = g.add("StudioAdainLatent", latent=ref(s1), reference=ref(ref_lat),
                   factor=float(p.drift_factor), skip_frames=int(skip), ramp_frames=int(p.drift_ramp))
    latent = g.add("LTXVLatentUpsampler", samples=ref(s1), upscale_model=ref(ups), vae=ref(vae))
    pinned = None
    if p.carry_tail:
        tail = g.add("StudioLoadImages", relpath=p.carry_tail)
        if p.carry_latent:
            src = g.add("StudioLoadLatent", relpath=p.carry_latent)
            latent = pinned = g.add("StudioLtxLatentCarry", vae=ref(vae), image=ref(tail), latent=ref(latent),
                                    source=ref(src), carry_latents=_carry_latents(p.carry_frames), strength=1.0)
        else:
            latent = pinned = g.add("LTXVImgToVideoInplace", vae=ref(vae), image=ref(tail), latent=ref(latent),
                                    strength=1.0, bypass=False)
        if p.pin_strengths:
            latent = g.add("StudioPinRamp", latent=ref(latent), strengths=p.pin_strengths)
    else:
        # the stage-1 video is non-zero everywhere: without a mask the keyframe node
        # would count every frame as occupied (the pin above sets one for later links)
        latent = g.add("StudioSetNoiseMask", latent=ref(latent), value=1.0)
    hp, hn, hl = pos, neg, ref(latent)          # (positive, negative, latent) the sampler sees
    if p.stage2_keyframes:
        kf_s1 = g.add("StudioLoadLatent", relpath=f"{p.draft}_s1_kf")
        kf_up = g.add("LTXVLatentUpsampler", samples=ref(kf_s1), upscale_model=ref(ups), vae=ref(vae))
        kf = g.add("LTXVAddGeneratedKeyframes", positive=pos, negative=neg, vae=ref(vae),
                   latent=ref(latent), interval_frames=int(p.keyframe_interval), keyframes=ref(kf_up))
        hp, hn, hl = ref(kf, 0), ref(kf, 1), ref(kf, 2)
    if use_msr and msr is not None:
        guide_inputs = {slot: ref(g.add("LoadImage", image=p.refs[slot]))
                        for slot in ("pic1", "pic2", "pic3", "pic4", "background") if p.refs.get(slot)}
        head = g.add("ComfyUILTX25MSRMultiReferenceGuide", positive=hp, negative=hn,
                     vae=ref(vae), latent=hl, strength=1.0, reference_frames=str(p.reference_frames),
                     use_tiled_encode=False, tile_size=256, tile_overlap=64, msr_parameters=ref(msr, 1),
                     **guide_inputs)
        hp, hn, hl = ref(head, 0), ref(head, 1), ref(head, 2)
    audio_s1 = g.add("StudioLoadLatent", relpath=f"{p.draft}_s1_audio")
    frozen = g.add("LTXVFreezeLatent", latent=ref(audio_s1))
    av = g.add("LTXVConcatAVLatent", video_latent=hl, audio_latent=ref(frozen))
    sigmas = g.add("ManualSigmas", sigmas=p.sigmas)
    sampler = g.add("KSamplerSelect", sampler_name=p.sampler)
    noise = g.add("RandomNoise", noise_seed=int(p.seed))
    guider = g.add("LTXVDualCFGGuider", model=ref(model), positive=hp, negative=hn,
                   video_cfg=1.0, audio_cfg=1.0)
    sampled = g.add("SamplerCustomAdvanced", noise=ref(noise), guider=ref(guider), sampler=ref(sampler),
                    sigmas=ref(sigmas), latent_image=ref(av))
    sep = g.add("LTXVSeparateAVLatent", av_latent=ref(sampled))
    pp, pn, video = hp, hn, ref(sep, 0)
    if p.stage2_keyframes:
        # peel the detailing keyframes first, then drop the MSR guide frames
        peel = g.add("LTXVSeparateGeneratedKeyframes", positive=hp, negative=hn,
                     latent=ref(sep, 0), keyframes_to_batch=False)
        pp, pn, video = ref(peel, 0), ref(peel, 1), ref(peel, 2)
    if use_msr:
        video = ref(g.add("LTXVCropGuides", positive=pp, negative=pn, latent=video), 2)
    if p.carry_tail and pinned and p.pin_strengths and p.carry_restore > 0:
        video = ref(g.add("StudioCarryRestore", latent=video, reference=ref(pinned),
                          carry_latents=_carry_latents(p.carry_frames), factor=float(p.carry_restore)), 0)
    if p.carry_tail and p.seam_adain > 0:
        video = ref(g.add("StudioSeamAdain", latent=video, carry_latents=_carry_latents(p.carry_frames),
                          ramp_latents=int(p.seam_ramp), factor=float(p.seam_adain), **_seam_mode(p.seam_mode)))
    g.add("StudioSaveLatent", latent=video, relpath=f"{p.out}_s2_video", save_noise_mask=False)
    evict = g.add("LTX25EvictModels", value=video, enabled=True, keep_text_encoder=True)
    frames = g.add("VAEDecodeTiled", samples=ref(evict), vae=ref(vae), tile_size=int(p.decode_tile),
                   overlap=64, temporal_size=64, temporal_overlap=16)
    audio = g.add("LTXVAudioVAEDecode", samples=ref(audio_s1), audio_vae=ref(avae))
    g.add("StudioSaveVideoLossless", images=ref(frames), fps=float(p.fps), relpath=f"{p.out}_final",
          audio=ref(audio))
    g.add("StudioSaveImages", images=ref(frames), relpath=f"{p.out}_final_tail", last_n=int(p.tail_out))
    return g.nodes


# The take switches to a new link at the first latent after its carry, and the 3
# carried latents after the anchor crossfade in latent space from the previous link's
# latents to the new link's re-render of them (weights 1/4, 2/4, 3/4). Switching
# on carried latent 2 without a blend (the earlier default) left the switch as its own
# small step next to the carry end (+3.1 saturation at one junction of the 1080p gate
# take); with the blend each link boundary is one event, every saturation step stays
# under 2, and the review strips show no ghosting (lab 2026-10-23, ltx-take-check on
# the stage-2 latents).
LTX_TAKE_JOIN = 4
LTX_TAKE_BLEND = 3
LTX_PIN_RAMP = "1.0,0.8,0.6,0.4"       # carried latent strengths, oldest first


def ltx_decode_take_graph(video_latents: list[str], out: str, carry_frames: int = LTX_CARRY_FRAMES,
                          join: int = LTX_TAKE_JOIN, blend: int = LTX_TAKE_BLEND, fps: float = 24.0,
                          decode_tile: int = 512, models: LtxModels | None = None) -> dict:
    """Join the links' video latents and decode the take once (streamed windows),
    so no link boundary is ever a pixel dissolve: the switch is one latent step
    (or a `blend`-latent crossfade in latent space) decoded with real context on
    both sides. Same decode tiling as the links."""
    g, m = _G(), models or LtxModels()
    vae = g.add("VAELoader", vae_name=m.vae)
    extra = {"blend_latents": int(blend)} if blend else {}
    g.add("StudioLtxDecodeTake", vae=ref(vae), links="\n".join(video_latents),
          carry_latents=_carry_latents(carry_frames), join_latent=int(join), fps=float(fps), relpath=out,
          tile_size=int(decode_tile), overlap=64, temporal_size=64, temporal_overlap=16,
          window_latents=24, context_latents=2, overlap_latents=2, **extra)
    return g.nodes


def ltx_audio_take_graph(audio_latents: list[str], frames: list[int], out: str,
                         carry_frames: int = LTX_CARRY_FRAMES, fps: float = 24.0,
                         models: LtxModels | None = None) -> dict:
    """Join the links' stage-1 audio latents (the carried latents are exact copies)
    and decode the take's sound once, so no audio seam exists to be hidden."""
    g, m = _G(), models or LtxModels()
    avae = g.add("LTXVAudioVAELoader", ckpt_name=m.audio_vae)
    take = g.add("StudioLtxAudioTake", links="\n".join(audio_latents),
                 frames=",".join(str(int(f)) for f in frames), carry_frames=int(carry_frames),
                 frame_rate=float(fps), audio_latents_per_second=25.0)
    audio = g.add("LTXVAudioVAEDecode", samples=ref(take, 0), audio_vae=ref(avae))
    g.add("StudioSaveAudio", audio=ref(audio), relpath=out, info=ref(take, 1))
    return g.nodes


# ========================================================================= H3
@dataclass
class H3Link:
    prompt: str
    seed: int
    frames: int
    out: str
    refs: list[str] = field(default_factory=list)       # ComfyUI input image names, <Picture N> order
    start_image: str | None = None                       # link 1: frame-0 guide image
    carry_from: str | None = None                        # previous link's AV latent relpath
    carry_frames: int = H3_CARRY_FRAMES
    width: int = 960
    height: int = 544
    steps: int = 32
    shift_video: float = 12.0
    shift_audio: float = 3.0
    attention: str = "comfy kitchen attention"
    loras: list[tuple[str, float]] = field(default_factory=list)
    decode_draft: bool = True
    # Off for H3: the studio's own H3 workflows do not release the encoder or the DiT
    # before sampling (their 1080p graphs keep only the upscaler pack's end-of-node
    # unloads), the broker restarts H3 rather than /free it (aimdo livelock), and the
    # studio box blue-screened inside the Windows GPU memory manager (dxgmms2.sys, in
    # the two dumps parsed) after in-process H3 model drops (2026-10-23;
    # chain_engine.H3Chain._fresh_if_dropping restarts the server before a graph that
    # would drop a model). Keeping the encoder and the DiT staged across the links of
    # one lease, as a run of normal H3 renders does, is also the fast path (link 4 of
    # the gate: first step 32.6 s, no reload of either).
    release_text_encoder: bool = False
    # Look match across the carry end (StudioSeamAdain path mode, phase by phase:
    # period H3_TOKEN_PERIOD), over seam_ramp tokens (5 = 17 frames).
    seam_adain: float = 0.0
    seam_ramp: int = 5
    models: H3Models = field(default_factory=H3Models)


@dataclass
class H3RefineLink:
    prompt: str
    frames: int               # window frames of this link (for the Ref2VA length input)
    start_frame: int          # first frame of this link's new content in the take


@dataclass
class H3Refine:
    links: list[str]                                     # AV latent relpaths, in order
    link_prompts: list[H3RefineLink]
    seed: int
    out: str
    refs: list[str] = field(default_factory=list)
    carry_frames: int = H3_CARRY_FRAMES
    seam_frames: list[int] = field(default_factory=list)
    base_width: int = 960
    base_height: int = 544
    width: int = 1920
    height: int = 1088
    sigmas: str = H3_REFINE_SIGMAS
    chunk_frames: int = 124
    overlap_frames: int = 22
    tile_width: int = 1024
    tile_height: int = 704
    seam_denoise: float = 0.8
    shift_video: float = 12.0
    shift_audio: float = 3.0
    attention: str = "comfy kitchen attention"
    loras: list[tuple[str, float]] = field(default_factory=list)
    models: H3Models = field(default_factory=H3Models)


def _h3_model(g: _G, m: H3Models, attention: str, sv: float, sa: float,
              loras: list[tuple[str, float]]) -> str:
    model = g.add("UNETLoader", unet_name=m.unet, weight_dtype="default")
    for name, strength in loras:
        model = g.add("LoraLoaderModelOnly", model=ref(model), lora_name=name, strength_model=float(strength))
    model = g.add("ModelAttentionBackend", model=ref(model), attention=attention)
    return g.add("MiniMaxH3SigmaShift", model=ref(model), shift_video=float(sv), shift_audio=float(sa))


def _h3_ref2va(g: _G, clip: str, vae: str, avae: str, prompt: str, w: int, h: int,
               frames: int, ref_nodes: list[str]) -> str:
    inputs = {f"ref_images.ref_image_{i}": ref(n) for i, n in enumerate(ref_nodes)}
    return g.add("MiniMaxH3ReferenceToVideo", clip=ref(clip), vae=ref(vae), audio_vae=ref(avae),
                 prompt=prompt, width=int(w), height=int(h), length=int(frames),
                 ref_image_size="match", **inputs)


def h3_seam_adain(g: _G, av: list, carry_frames: int, ramp: int, factor: float) -> list:
    """The sampled AV latent with its video's look eased across the carry end: the
    first new tokens follow, phase by phase, the line from the carry's look to the
    look the new content settles to (the carry and the audio are untouched)."""
    return ref(g.add("StudioSeamAdain", latent=av, carry_latents=h3_tokens(carry_frames),
                     ramp_latents=int(ramp), factor=float(factor), mode="path", period=H3_TOKEN_PERIOD))


def h3_link_graph(p: H3Link) -> dict:
    g, m = _G(), p.models
    model = _h3_model(g, m, p.attention, p.shift_video, p.shift_audio, p.loras)
    clip = g.add("CLIPLoader", clip_name=m.clip, type="minimax", device="default")
    vae = g.add("VAELoader", vae_name=m.vae)
    avae = g.add("VAELoader", vae_name=m.audio_vae)
    ref_nodes = [g.add("LoadImage", image=name) for name in p.refs]
    r2v = _h3_ref2va(g, clip, vae, avae, p.prompt, p.width, p.height, p.frames, ref_nodes)
    cond, latent = ref(r2v, 0), ref(r2v, 1)
    if p.release_text_encoder:
        cond = _release_text_encoder(g, cond, encoder_only=True)   # keep the warm DiT
    if p.carry_from:
        src = g.add("StudioLoadLatent", relpath=p.carry_from)
        latent = ref(g.add("StudioH3CarryPrefix", latent=ref(r2v, 1), source=ref(src),
                           carry_frames=int(p.carry_frames)))
    elif p.start_image:
        img = g.add("LoadImage", image=p.start_image)
        cond = ref(g.add("MiniMaxH3AddGuide", positive=cond, latent=ref(r2v, 1), image=ref(img),
                         vae=ref(vae), frame_idx=0))
    sampler = g.add("KSamplerSelect", sampler_name="euler")
    noise = g.add("RandomNoise", noise_seed=int(p.seed))
    guider = g.add("BasicGuider", model=ref(model), conditioning=cond)
    sched = g.add("BasicScheduler", model=ref(model), scheduler="simple", steps=int(p.steps), denoise=1.0)
    sampled = ref(g.add("SamplerCustomAdvanced", noise=ref(noise), guider=ref(guider), sampler=ref(sampler),
                        sigmas=ref(sched), latent_image=latent))
    if p.carry_from and p.seam_adain > 0:
        sampled = h3_seam_adain(g, sampled, p.carry_frames, p.seam_ramp, p.seam_adain)
    g.add("StudioSaveLatent", latent=sampled, relpath=f"{p.out}_av", save_noise_mask=False)
    if p.decode_draft:
        frames = g.add("VAEDecode", samples=sampled, vae=ref(vae))
        audio = g.add("VAEDecodeAudio", samples=sampled, vae=ref(avae))
        g.add("StudioSaveVideoLossless", images=ref(frames), fps=24.0, relpath=f"{p.out}_draft",
              audio=ref(audio))
    return g.nodes


def h3_decode_take_graph(links: list[str], out: str, carry_frames: int = H3_CARRY_FRAMES,
                         models: H3Models | None = None) -> dict:
    """Join the base-resolution links into one take latent and decode it once: the
    carry is exact in latent space, so a single decode leaves no decoder seam. With
    one latent (the refined take's) it decodes that latent as it is."""
    g, m = _G(), models or H3Models()
    vae = g.add("VAELoader", vae_name=m.vae)
    avae = g.add("VAELoader", vae_name=m.audio_vae)
    take = g.add("StudioH3ConcatTake", links="\n".join(links), carry_frames=int(carry_frames))
    frames = g.add("VAEDecode", samples=ref(take, 0), vae=ref(vae))
    audio = g.add("VAEDecodeAudio", samples=ref(take, 0), vae=ref(avae))
    g.add("StudioSaveVideoLossless", images=ref(frames), fps=24.0, relpath=f"{out}_take_final", audio=ref(audio))
    return g.nodes


def h3_refine_cond_rels(p: H3Refine) -> list[str]:
    """Where h3_refine_cond_graph saves each link's conditioning."""
    return [f"{p.out}_cond{j + 1}" for j in range(len(p.link_prompts))]


def _h3_refine_check(p: H3Refine) -> None:
    if len(p.link_prompts) != len(p.links) or not p.links:
        raise ValueError("one prompt per link")
    if len(p.links) > 8:
        raise ValueError("the conditioning schedule takes at most 8 links per refine")


def h3_refine_cond_graph(p: H3Refine) -> dict:
    """Encode each link's prompt with the references (as the link sampled it, at the
    base size) and save the conditionings, so the refine samples in a server that
    never loads the text encoder (~15 GB of RAM next to the DiT otherwise)."""
    _h3_refine_check(p)
    g, m = _G(), p.models
    clip = g.add("CLIPLoader", clip_name=m.clip, type="minimax", device="default")
    vae = g.add("VAELoader", vae_name=m.vae)
    avae = g.add("VAELoader", vae_name=m.audio_vae)
    ref_nodes = [g.add("LoadImage", image=name) for name in p.refs]
    for lp, rel in zip(p.link_prompts, h3_refine_cond_rels(p)):
        r2v = _h3_ref2va(g, clip, vae, avae, lp.prompt, p.base_width, p.base_height, lp.frames, ref_nodes)
        g.add("StudioSaveConditioning", conditioning=ref(r2v, 0), relpath=rel)
    return g.nodes


def h3_refine_graph(p: H3Refine) -> dict:
    """The 1080p refine of the joined take from the saved link conditionings
    (h3_refine_cond_graph); the server holds the DiT and the upscaler only."""
    _h3_refine_check(p)
    g, m = _G(), p.models
    model = _h3_model(g, m, p.attention, p.shift_video, p.shift_audio, p.loras)
    conds = {f"cond_{j + 1}": ref(g.add("StudioLoadConditioning", relpath=rel))
             for j, rel in enumerate(h3_refine_cond_rels(p))}
    schedule = ref(g.add("StudioH3CondSchedule",
                         start_frames=",".join(str(int(lp.start_frame)) for lp in p.link_prompts), **conds))
    take = g.add("StudioH3ConcatTake", links="\n".join(p.links), carry_frames=int(p.carry_frames))
    sep = g.add("LTXVSeparateAVLatent", av_latent=ref(take, 0))
    up = g.add("MinimaxH3LatentUpscaler3D", latent=ref(sep, 0), model_name=m.upscaler,
               mode="target dimensions", **{"mode.width": int(p.width), "mode.height": int(p.height)},
               align=32, enable_temporal_chunking=True, force_unload=True, device="cuda", precision="bf16")
    joined = g.add("LTXVConcatAVLatent", video_latent=ref(up), audio_latent=ref(sep, 1))
    tparams = g.add("MMH3TemporalSplitParamsV10", chunk_frames=int(p.chunk_frames),
                    temporal_overlap_frames=int(p.overlap_frames), anchor_strength=0.999,
                    motion_anchor_frames="0", identity_anchor_frames=0)
    sparams = g.add("MMH3SpatialSplitParamsV10", tile_width=int(p.tile_width), tile_height=int(p.tile_height),
                    overlap_ratio=0.25, fade_ratio=0.5, min_tile_size=256, seam_denoise=float(p.seam_denoise))
    noise = g.add("RandomNoise", noise_seed=int(p.seed))
    sigmas = g.add("ManualSigmas", sigmas=p.sigmas)
    sampler = g.add("KSamplerSelect", sampler_name="euler")
    refined = g.add("MMH3SplitUpscale", model=ref(model), conditioning=conds["cond_1"], latent=ref(joined),
                    noise=ref(noise), sampler=ref(sampler), sigmas=ref(sigmas), cfg=1.0,
                    temporal_split_param=ref(tparams), spatial_split_param=ref(sparams), seam_polish="off",
                    color_match=True, temporal_mode="pin",
                    protected_frames=",".join(str(int(f)) for f in p.seam_frames),
                    cond_schedule=schedule)
    # no decode here: the 1080p take is decoded by h3_decode_take_graph in its own step
    # (H3Chain.refine), away from the DiT this graph holds
    g.add("StudioSaveLatent", latent=ref(refined), relpath=f"{p.out}_take_av", save_noise_mask=False)
    return g.nodes
