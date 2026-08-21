"""Download EXAONE as ordinary files, avoiding Windows cache symlinks."""

from pathlib import Path

import config  # Load .env before importing huggingface_hub constants.

from huggingface_hub import snapshot_download


def main() -> None:
    config.validate_required_config(("model",))
    snapshot_download(config.MODEL_ID, local_dir=config.MODEL_PATH)
    model_path = Path(config.MODEL_PATH)
    required = ("config.json", "tokenizer_config.json", "tokenizer.json")
    missing = [name for name in required if not (model_path / name).is_file()]
    if missing or not any(model_path.glob("*.safetensors")):
        raise RuntimeError(
            "Downloaded model snapshot is incomplete: "
            + ", ".join(missing or ["*.safetensors"])
        )
    print("Configured EXAONE model snapshot is present as ordinary local files.")


if __name__ == "__main__":
    main()
