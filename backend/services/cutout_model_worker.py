from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import onnxruntime as ort
from PIL import Image, ImageOps


def main() -> None:
    model_path, input_path, output_path = map(Path, sys.argv[1:4])
    image = ImageOps.exif_transpose(Image.open(input_path)).convert("RGB")
    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    session = ort.InferenceSession(
        str(model_path),
        sess_options=options,
        providers=["CPUExecutionProvider"],
    )
    model_input = session.get_inputs()[0]
    input_height = int(model_input.shape[2])
    input_width = int(model_input.shape[3])
    resized = np.asarray(
        image.resize(
            (input_width, input_height),
            Image.Resampling.LANCZOS,
        ),
        dtype=np.float32,
    ) / 255.0
    if "isnet" in model_path.name.lower():
        mean = np.array([0.5, 0.5, 0.5], dtype=np.float32)
        std = np.array([1.0, 1.0, 1.0], dtype=np.float32)
    else:
        mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
        std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
    tensor = ((resized - mean) / std).transpose(2, 0, 1)[None]
    prediction = session.run(
        [session.get_outputs()[0].name],
        {model_input.name: tensor},
    )[0][0, 0]
    if "birefnet" in model_path.name.lower():
        prediction = 1.0 / (1.0 + np.exp(-prediction))
    prediction -= float(prediction.min())
    prediction /= max(1e-8, float(prediction.max()))
    matte = Image.fromarray(
        np.clip(prediction * 255.0, 0, 255).astype(np.uint8),
        "L",
    ).resize(image.size, Image.Resampling.LANCZOS)
    matte.save(output_path)


if __name__ == "__main__":
    main()
