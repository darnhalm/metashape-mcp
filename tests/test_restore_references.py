import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path

sys.modules.setdefault("Metashape", types.ModuleType("Metashape"))

from restore_points import missing_references, new_unreferenced_folders  # noqa: E402


def write_zip(path, doc_paths, members=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    xml = "<frame>" + "".join(f'<x path="{item}"/>' for item in doc_paths) + "</frame>"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("doc.xml", xml)
        for member in members:
            archive.writestr(member, b"data")


class MissingReferencesTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.data = self.root / "probe.files"
        self.snapshot = self.root / "snapshot"
        frame_refs = ["model/model.zip", "depth_maps/depth_maps.zip", "point_cloud/point_cloud.zip",
                      "../../../photos/IMG_1.jpg"]
        write_zip(self.snapshot / "project.files/2/0/frame.zip", frame_refs)
        write_zip(self.data / "2/0/model/model.zip", ["mesh.ply"], ["mesh.ply"])
        write_zip(self.data / "2/0/depth_maps/depth_maps.zip", ["data0.zip"])
        write_zip(self.data / "2/0/depth_maps/data0.zip", [])
        write_zip(self.data / "2/0/point_cloud/point_cloud.zip", ["p0.ply"], ["p0.ply"])
        self.target = {"snapshot_directory": str(self.snapshot),
                       "project_path": str(self.root / "probe.psx"),
                       "metadata_files": ["2/0/frame.zip"]}

    def test_complete_data_has_no_missing_files(self):
        self.assertEqual(missing_references(self.target), [])

    def test_missing_tie_points_and_sibling_file_are_reported(self):
        (self.data / "2/0/point_cloud/point_cloud.zip").unlink()
        (self.data / "2/0/depth_maps/data0.zip").unlink()
        self.assertEqual(missing_references(self.target),
                         ["2/0/depth_maps/data0.zip", "2/0/point_cloud/point_cloud.zip"])

    def test_data_moved_to_quarantine_counts_as_present(self):
        quarantine = self.root / ".metashape-mcp-quarantine/probe.chunk2"
        quarantine.parent.mkdir()
        (self.data / "2").rename(quarantine)
        self.target["moved_data"] = [{"original_path": str(self.data / "2"),
                                      "quarantine_path": str(quarantine)}]
        self.assertEqual(missing_references(self.target), [])
        (quarantine / "0/point_cloud/point_cloud.zip").unlink()
        self.assertEqual(missing_references(self.target), ["2/0/point_cloud/point_cloud.zip"])


class NewUnreferencedFoldersTest(unittest.TestCase):
    def test_only_new_and_unreferenced_folders_qualify(self):
        root = Path(tempfile.mkdtemp())
        data = root / "probe.files"
        snapshot = root / "snapshot/project.files"
        write_zip(snapshot / "project.zip", ["0/chunk.zip"])
        write_zip(snapshot / "0/0/frame.zip", ["dense_cloud/dense_cloud.zip", "model/model.zip"])
        for folder in ("dense_cloud", "model", "old_orphan", "dense_cloud.1"):
            (data / "0/0" / folder).mkdir(parents=True)
            (data / "0/0" / folder / "x.bin").write_bytes(b"1")
        (data / "5/0").mkdir(parents=True)
        (data / "5/0/frame.zip").write_bytes(b"1")
        recorded = {"0/0/dense_cloud/x.bin": [1, 1], "0/0/model/x.bin": [1, 1],
                    "0/0/old_orphan/x.bin": [1, 1]}
        target = {"snapshot_directory": str(root / "snapshot"), "project_path": str(root / "probe.psx"),
                  "data_files": recorded}
        found = [path.relative_to(data).as_posix() for path in new_unreferenced_folders(target)]
        # new cloud version and the chunk of a job copy go; referenced and pre-existing folders stay
        self.assertEqual(found, ["0/0/dense_cloud.1", "5"])


if __name__ == "__main__":
    unittest.main()
