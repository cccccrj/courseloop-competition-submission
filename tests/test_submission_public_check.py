"""Public-delivery checks distinguish real deployment identifiers from fixtures."""
import importlib.util
import hashlib
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('public_check',ROOT/'scripts/submission_public_check.py')
check=importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)

class PublicDeliveryChecks(unittest.TestCase):
    def test_owner_exception_is_exact_hash_and_domain_only(self):
        data=('https://demo'+'.pubs'+'.net'+'.cn/').encode()
        approved={'final.txt':hashlib.sha256(data).hexdigest()}
        result=check.scan_blobs([('final.txt',data)],[],False,approved)
        self.assertFalse(result['findings'])
        self.assertTrue(result['owner_approved_platform_domain_findings'])
        self.assertTrue(check.scan_blobs([('final.txt',data+b'changed')],[],False,approved)['findings'])
        self.assertTrue(check.scan_blobs([('another.txt',data)],[],False,approved)['findings'])
        # Explicit display exceptions never suppress credential findings.
        result=check.scan_blobs([('final.txt',data)],[data],False,approved)
        self.assertEqual('protected_reviewer_password',result['findings'][0]['category'])

    def test_real_platform_domain_is_rejected(self):
        domain='https://demo'+'.pubs'+'.net'+'.cn/'
        self.assertTrue(check.inspect_text('report',domain.encode()))
        self.assertEqual([],check.inspect_text('report',b'https://review-endpoint.example.invalid'))

    def test_deployment_location(self):
        self.assertTrue(check.inspect_text('report', '\u6821\u56ed\u673a'.encode()))
        self.assertTrue(check.inspect_text('report', '\u751f\u4ea7\u6570\u636e\u76d8\u70b9'.encode()))

    def test_actual_working_path(self):
        value='/'+ 'root' + '/project/model'
        self.assertTrue(check.inspect_text('log',value.encode()))

    def test_local_addresses_and_versions_are_not_host_leaks(self):
        self.assertEqual([],check.inspect_text('example',b'http://127.0.0.1:8000 10.2.1 192.168.1.1'))

    def test_portable_paths_and_hardware_are_allowed(self):
        self.assertEqual([],check.inspect_text('report',b'/opt/projects/demo/model RTX 4090D 64 CPU 128GB'))

    def test_no_secret_is_echoed(self):
        secret=b'fixture-password-not-real'
        findings=check.inspect_text('ACCESS',secret,[secret])
        self.assertEqual('protected_reviewer_password',findings[0]['category'])
        self.assertNotIn(secret.decode(),str(findings))

    def test_data_attribution_is_preserved(self):
        self.assertEqual([],check.inspect_text('data','部分数据来源于南京理工大学计算机科学与工程学院本科生自建数据库。'.encode()))

    def test_business_and_course_content_is_not_redacted(self):
        data=b'SQLite format 3\x00'+'\u6821\u56ed招聘和网络教学示例'.encode()
        self.assertEqual([],check.inspect_text('data.db',data))

if __name__=='__main__':unittest.main()
