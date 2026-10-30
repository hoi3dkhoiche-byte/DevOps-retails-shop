"""Read actual fixture config IDs without trusting Docker's index identifier."""
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

import images


class FixtureArchiveTests(unittest.TestCase):
    def archive(self, tag="mysql:8.4.11", *, architecture="amd64", config_name="config.json",
                duplicate_config=False, symlink_config=False):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "fixture.tar"
        config = json.dumps({"os": "linux", "architecture": architecture, "rootfs": {}}).encode()
        manifest = json.dumps([{"Config": config_name, "RepoTags": [tag], "Layers": []}]).encode()
        with tarfile.open(path, "w") as archive:
            for name, data in (("manifest.json", manifest), (config_name, config)):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                if name == config_name and symlink_config:
                    info.type, info.linkname, info.size = tarfile.SYMTYPE, "outside.json", 0
                    archive.addfile(info)
                else:
                    archive.addfile(info, io.BytesIO(data))
                if name == config_name and duplicate_config:
                    archive.addfile(info, io.BytesIO(data))
        return path, "sha256:" + hashlib.sha256(config).hexdigest()

    def test_official_docker_hub_aliases_return_actual_config_digest(self):
        for tag in ("mysql:8.4.11", "library/mysql:8.4.11", "docker.io/library/mysql:8.4.11"):
            with self.subTest(tag=tag):
                archive, expected = self.archive(tag)
                self.assertEqual(images.archive_config_id(archive, "docker.io/library/mysql:8.4.11"), expected)

    def test_wrong_registry_namespace_tag_or_extra_library_prefix_fails(self):
        for tag in ("ghcr.io/library/mysql:8.4.11", "example/mysql:8.4.11", "mysql:8.4.10"):
            with self.subTest(tag=tag):
                archive, _ = self.archive(tag)
                with self.assertRaisesRegex(images.ContractError, "Archive tag differs"):
                    images.archive_config_id(archive, "docker.io/library/mysql:8.4.11")
        archive, _ = self.archive("docker.io/library/hmannnguye/devops-retails-shop:release")
        with self.assertRaisesRegex(images.ContractError, "Archive tag differs"):
            images.archive_config_id(archive, "docker.io/hmannnguye/devops-retails-shop:release")
        archive, _ = self.archive("docker.io/ghcr.io/mysql:8.4.11")
        with self.assertRaisesRegex(images.ContractError, "Archive tag differs"):
            images.archive_config_id(archive, "ghcr.io/mysql:8.4.11")

    def test_wrong_platform_unsafe_path_duplicate_and_symlink_config_fail(self):
        for options in ({"architecture": "arm64"}, {"config_name": "../outside.json"},
                        {"duplicate_config": True}, {"symlink_config": True}):
            with self.subTest(options=options):
                archive, _ = self.archive(**options)
                with self.assertRaises(images.ContractError):
                    images.archive_config_id(archive, "docker.io/library/mysql:8.4.11")

    def test_application_archive_still_requires_recorded_config_digest(self):
        archive, actual = self.archive("hmannnguye/devops-retails-shop:release")
        image = {"local_tag": "docker.io/hmannnguye/devops-retails-shop:release", "image_id": actual}
        images.archive_config(archive, image)
        image["image_id"] = "sha256:" + "f" * 64
        with self.assertRaisesRegex(images.ContractError, "Archive config differs from recorded image ID"):
            images.archive_config(archive, image)


if __name__ == "__main__":
    unittest.main()
