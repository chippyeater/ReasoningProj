"""独立运行：验证真实 DOCX 经当前 backend/app/case_service.py 解析所得文本。

需要安装后端依赖，并将原始 DOCX 放在 fixtures/raw/。
目前仅检查解析器内容及失败状态，不能代替完整上传/解析文本持久化集成测试。
运行: python evals/case_a/tests/check_docx_parsing.py
"""
import sys,hashlib,json,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; REPO=ROOT.parents[1]

class TestRawDocxParsing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        backend=REPO/"backend"
        if not backend.is_dir():
            raise RuntimeError(f"未找到后端目录 {backend}，需将 evals/ 放到 ReasoningProj 仓库根目录下")
        sys.path.insert(0,str(backend))
        try:
            from app.case_service import _extract_content,_guess_parse_status
            from app.schemas import FileType
        except Exception as exc:
            raise RuntimeError(f"加载实际后端解析器失败，请安装后端依赖: {exc}") from exc
        cls.extract=_extract_content; cls.status=_guess_parse_status; cls.filetype=FileType
        cls.cases=[json.loads(line) for line in (ROOT/"gold/gold_parse_cases.jsonl").read_text(encoding="utf-8").splitlines() if line.strip() and '"stage":"parse"' in line]

    def find_docx(self,name):
        path=ROOT/"fixtures/raw"/name
        if path.is_file():return path
        self.fail(f"缺少原始 DOCX: {name}。请放入 fixtures/raw/")

    def test_five_docx_content_matches_gold(self):
        for case in self.cases:
            if case["id"] not in {"P001","P002","P003","P004","P005"}:continue
            name=case["input"]["filename"]; p=self.find_docx(name)
            with self.subTest(name=name):
                content=self.extract(name,None,p.read_bytes())
                self.assertFalse(content.startswith("["),"解析返回错误/占位内容")
                self.assertEqual(hashlib.sha256(content.encode("utf8")).hexdigest(),case["expected"]["text_sha256"],"与固定Gold文本不一致；请定位段落与换行差异，不要自动覆盖来源位置")
                self.assertEqual(len(content),case["expected"]["text_length"])

    def test_corrupt_docx_not_reported_success(self):
        content=self.extract('broken.docx',None,b'not a docx file')
        status=self.status(self.filetype.docx,content)
        status_value=getattr(status,'value',str(status))
        self.assertNotEqual(status_value,'parsed',"已知风险：DOCX解析失败时不能返回parsed状态")

if __name__=='__main__':unittest.main()
