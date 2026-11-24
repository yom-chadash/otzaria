"""Corruption/relocation contracts for post-validation, with no native run."""
import copy
import hashlib
from pathlib import Path
import stat
import tempfile
import unittest
import zipfile

import font_fallback_fixtures as fixtures
import font_fallback_replay as replay


class ReplayContracts(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def archive(self, name='raw/result.json', kind=stat.S_IFREG):
        path = self.root / 'input.zip'
        with zipfile.ZipFile(path, 'w') as output:
            info = zipfile.ZipInfo(name)
            info.external_attr = (kind | 0o600) << 16
            output.writestr(info, b'{"original":true}\n')
        return path, {'zipSizeBytes': path.stat().st_size, 'zipSha256': replay.digest(path), 'memberCount': 1}

    def test_exact_zip_and_original_member_bytes_are_required(self):
        archive, spec = self.archive()
        output = self.root / 'out'
        manifest = replay.extract_verified(archive, spec, output)
        replay.verify_members(output, manifest)
        (output / 'raw/result.json').write_bytes(b'{"original":false}\n')
        with self.assertRaisesRegex(ValueError, 'member drifted'):
            replay.verify_members(output, manifest)

    def test_wrong_zip_digest_is_rejected_before_extraction(self):
        archive, spec = self.archive()
        spec['zipSha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'ZIP hash/size'):
            replay.extract_verified(archive, spec, self.root / 'out')
        self.assertFalse((self.root / 'out').exists())

    def test_paths_and_nonregular_members_cannot_escape(self):
        for name, kind in [('../outside', stat.S_IFREG), ('/absolute', stat.S_IFREG),
                           ('raw/link', stat.S_IFLNK), ('raw/device', stat.S_IFCHR)]:
            archive, spec = self.archive(name, kind)
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'artifact member'):
                replay.extract_verified(archive, spec, self.root / 'out')
            self.assertFalse((self.root / 'out').exists())

    def fixture(self):
        data = fixtures.pdf('OnlyThisDocument')
        local = self.root / 'fixtures/case/source.pdf'
        local.parent.mkdir(parents=True)
        local.write_bytes(data)
        item = {'id': 'source-0', 'path': 'case/source.pdf', 'sha256': hashlib.sha256(data).hexdigest(),
                'sizeBytes': len(data), 'pageCount': 1, 'targetPage': 1,
                'requestedFace': 'OnlyThisDocument', 'expectedWord': 'שלום',
                'fontResourcesByPage': {'1': ['OnlyThisDocument', 'Helvetica']},
                'expectedControlText': 'Standard14 control'}
        config = {'scenario': 'case', 'documents': [{**item, 'absolutePath': '/original/fixtures/case/source.pdf'}]}
        manifest = {'cases': [{'id': 'case', 'documents': [item]}]}
        return config, manifest, local

    def test_fixture_mapping_preserves_original_config_and_owner_path(self):
        config, manifest, _ = self.fixture()
        original = copy.deepcopy(config)
        mapping = {}
        replay.fixture_mapping(self.root, config, manifest, mapping)
        source = Path(__file__).with_name('run_font_fallback_profile.py')
        functions = replay.load_validation_functions(source, replay.digest(source), mapping)
        resources = functions['fixture_font_resources'](config['documents'][0])
        self.assertEqual(resources, {'1': ['Helvetica', 'OnlyThisDocument']})
        self.assertEqual(config, original)
        self.assertEqual(set(mapping), {'/original/fixtures/case/source.pdf'})

    def test_changed_fixture_bytes_or_manifest_are_rejected(self):
        config, manifest, local = self.fixture()
        local.write_bytes(local.read_bytes() + b' ')
        with self.assertRaisesRegex(ValueError, 'fixture hash/size'):
            replay.fixture_mapping(self.root, config, manifest, {})
        config['documents'][0]['pageCount'] = 2
        with self.assertRaisesRegex(ValueError, 'manifest differs'):
            replay.fixture_mapping(self.root, config, manifest, {})

    def test_validator_source_is_pinned_and_unknown_original_paths_fail(self):
        source = Path(__file__).with_name('run_font_fallback_profile.py')
        with self.assertRaisesRegex(ValueError, 'validator source SHA'):
            replay.load_validation_functions(source, '0' * 64, {})
        functions = replay.load_validation_functions(source, replay.digest(source), {})
        with self.assertRaisesRegex(ValueError, 'unverified original PDF path'):
            functions['fixture_font_resources']({'absolutePath': '/arbitrary/file'})


if __name__ == '__main__':
    unittest.main()
