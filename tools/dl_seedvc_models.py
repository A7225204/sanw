import os, time
from huggingface_hub import hf_hub_download
jobs = [
    ("lj1995/VoiceConversionWebUI", "rmvpe.pt"),
    ("FunAudioLLM/CosyVoice-300M", "hift.pt"),
    ("Plachta/Seed-VC", "config_dit_mel_seed_uvit_whisper_base_f0_44k.yml"),
    ("Plachta/Seed-VC", "DiT_seed_v2_uvit_whisper_base_f0_44k_bigvgan_pruned_ft_ema_v2.pth"),
]
for repo, fn in jobs:
    t0 = time.time()
    try:
        p = hf_hub_download(repo_id=repo, filename=fn, cache_dir="./checkpoints")
        print(f"[OK] {fn} {os.path.getsize(p)/1e6:.1f}MB {time.time()-t0:.0f}s", flush=True)
    except Exception as e:
        print(f"[失败] {fn}: {type(e).__name__}: {str(e)[:120]}", flush=True)
