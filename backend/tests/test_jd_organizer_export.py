import zipfile

from PIL import Image

from backend.services import vip_organizer_service as service


def test_jd_export_uses_separate_800_and_750_folders(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "_session_result_dir", lambda _session_id: tmp_path)
    monkeypatch.setattr(service, "_validate_slot_map", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        service,
        "_render_slot_image",
        lambda *_args, **_kwargs: Image.new("RGB", (32, 32), "white"),
    )

    slots = [
        {"file_name": file_name, "image_ids": [1], "adjustments": []}
        for file_name, *_ in service.JD_SLOT_DEFINITIONS
    ]
    session_id = "a" * 32
    result = service.export_package(session_id, slots, {}, "jd")
    export_id = result["download_url"].split("/")[-2]
    zip_path = service.export_zip(session_id, export_id)

    with zipfile.ZipFile(zip_path) as archive:
        names = set(archive.namelist())

    expected_800 = {
        "800/0-无logo.jpg",
        "800/1.jpg",
        "800/2.jpg",
        "800/3.jpg",
        "800/4.jpg",
        "800/5.jpg",
        "800/透明.png",
    }
    expected_750 = {f"750/{index}.jpg" for index in range(1, 6)}

    assert names == expected_800 | expected_750
    assert all(name.startswith(("800/", "750/")) for name in names)
