"""正式报告生成入口：只读取最终提交版本证据。"""
from pathlib import Path
import runpy

if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).with_name("submission_patch_report.py")),run_name="__main__")
