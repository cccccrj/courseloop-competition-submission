"""The competition docs tree contains formal delivery materials only."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]

class FormalDeliveryScope(unittest.TestCase):
    def test_docs_root_contains_only_formal_and_diagrams(self):
        self.assertEqual({'submission', 'diagrams'}, {p.name for p in (ROOT/'docs').iterdir() if p.is_dir()})
        self.assertFalse(list((ROOT/'docs').glob('*.md')))
        self.assertFalse((ROOT/'docs/submission/historical').exists() and any((ROOT/'docs/submission/historical').iterdir()))

    def test_formal_reports_have_three_formats(self):
        directory = ROOT/'docs/submission'
        for number in range(9):
            for extension in ('md', 'docx', 'pdf'):
                self.assertEqual(1, len(list(directory.glob(f'{number:02d}-*.{extension}'))))

    def test_no_intermediate_links_or_requested_wording(self):
        files = [ROOT/'README.md', ROOT/'RELEASE-NOTES.md', ROOT/'PUBLICATION.md', *(ROOT/'docs/submission').glob('*.md')]
        for path in files:
            text = path.read_text()
            for fragment in ('historical/', '提交前检查清单', '完成确认前仓库保持私有', '所有者确认保留PPT末页'):
                self.assertNotIn(fragment, text, str(path))

if __name__ == '__main__':
    unittest.main()
