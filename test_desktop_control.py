import unittest
from unittest.mock import patch
from desktop_control import ROOT, is_our_process, main


class LauncherCheck(unittest.TestCase):
    def test_only_exact_project_process_matches(self):
        with patch('pathlib.Path.read_bytes', return_value=b'python\0' + str(ROOT / 'auto_fishing.py').encode() + b'\0'):
            self.assertTrue(is_our_process(123))
        with patch('pathlib.Path.read_bytes', return_value=b'python\0/another/auto_fishing.py\0'):
            self.assertFalse(is_our_process(123))
        with patch('pathlib.Path.read_bytes', side_effect=FileNotFoundError):
            self.assertFalse(is_our_process(123))

    def test_invalid_action_rejected_before_desktop_access(self):
        with self.assertRaises(SystemExit):
            main('invalid')


if __name__ == '__main__':
    unittest.main()
