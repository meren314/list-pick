"""Independent acceptance checks using synthetic files, never customer data."""

import csv
import hashlib
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

import core


class ProductFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="list-pick-acceptance-")
        self.base = Path(self.temp.name)
        self.source = self.base / "source"
        self.output_parent = self.base / "output"
        self.source.mkdir()
        self.output_parent.mkdir()
        self.fixture = [
            "docs/财务_2026_01.pdf", "docs/财务_2026_02.PDF",
            "archive/财务_2026_01.pdf", "archive/财务_2025_12.pdf",
            "notes/财务_2026_说明.txt", "notes/README",
            "notes/archive.tar.gz", "docs/外联_2026_01.docx",
        ]
        for relative in self.fixture:
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(("synthetic acceptance file: " + relative).encode("utf-8"))

    def tearDown(self):
        self.temp.cleanup()

    def snapshot(self):
        return {str(path.relative_to(self.source)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.source.rglob("*") if path.is_file()}

    def report(self, exported):
        self.assertEqual(exported["report_error"], "")
        with open(exported["report_path"], encoding="utf-8-sig", newline="") as stream:
            return list(csv.DictReader(stream))

    def copies(self, exported):
        output = Path(exported["output_dir"])
        report = Path(exported["report_path"])
        return {str(path.relative_to(output)): path.read_bytes()
                for path in output.rglob("*") if path.is_file() and path != report}

    def choose(self, row, candidates=None):
        row.update(status="selected", selected_candidates=list(candidates or row["candidates"]))

    def assert_output(self, exported, expected):
        actual = self.copies(exported)
        expected = {str(Path(relative)) for relative in expected}
        self.assertEqual(set(actual), expected)
        self.assertEqual(exported["copied"], len(expected))
        for relative, data in actual.items():
            self.assertEqual(data, (self.source / relative).read_bytes())
        report_text = "\n".join(str(value) for row in self.report(exported) for value in row.values())
        self.assertIn(f"成功复制：{len(expected)}", report_text)

    def test_regex_one_to_many_overlapping_rules_copy_union_once(self):
        before = self.snapshot()
        result = core.scan(str(self.source), r"^财务_2026_\d{2}\.pdf$" + "\n" + r"\.pdf$", mode="regex")
        first, second = result["rows"]
        self.assertEqual([first["status"], second["status"]], ["multiple", "multiple"])
        self.assertEqual([len(first["candidates"]), len(second["candidates"])], [3, 4])
        self.choose(first)
        self.choose(second)
        exported = core.export_result(result, str(self.output_parent))
        self.assertEqual(exported["failed"], 0)
        self.assert_output(exported, self.fixture[:4])
        self.assertEqual(self.snapshot(), before)
        rows = [row for row in self.report(exported) if row["记录类型"] == "清单项"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(set(rows[0]["实际选择路径"].splitlines()), set(first["candidates"]))
        self.assertEqual(set(rows[1]["实际选择路径"].splitlines()), set(second["candidates"]))

    def test_multiple_initially_unselected_and_explicit_subset_only(self):
        result = core.scan(str(self.source), "pdf", mode="extension")
        row = result["rows"][0]
        self.assertEqual(row["status"], "multiple")
        initial = core.export_result(result, str(self.output_parent))
        self.assert_output(initial, [])
        chosen = str(self.source / self.fixture[0])
        self.choose(row, [chosen])
        selected = core.export_result(result, str(self.output_parent))
        self.assert_output(selected, [self.fixture[0]])
        row["status"] = "multiple"
        row.pop("selected_candidates")
        cleared = core.export_result(result, str(self.output_parent))
        self.assert_output(cleared, [])

    def test_extension_notation_duplicates_no_suffix_and_filters(self):
        result = core.scan(str(self.source), "pdf\n.PDF\n*.pdf\n[无后缀]\n.gz", mode="extension")
        self.assertTrue(result["complete"])
        self.assertEqual([row["status"] for row in result["rows"]],
                         ["multiple", "duplicate", "duplicate", "unique", "unique"])
        self.assertEqual(len(result["rows"][0]["candidates"]), 4)
        self.assertEqual(Path(result["rows"][3]["candidates"][0]).name, "README")
        self.assertEqual(Path(result["rows"][4]["candidates"][0]).name, "archive.tar.gz")
        filtered = core.scan(str(self.source), ".", mode="regex",
                             include_extensions=".pdf;txt;[无后缀]", exclude_extensions="pdf;txt")
        self.assertTrue(filtered["complete"])
        self.assertEqual(filtered["rows"][0]["candidates"], [str(self.source / "notes/README")])
        self.assert_output(core.export_result(filtered, str(self.output_parent)), ["notes/README"])

    def test_bad_regex_blocks_copy_report_and_corrected_rescan_recovers(self):
        result = core.scan(str(self.source), "README\n(", mode="regex")
        self.assertFalse(result["complete"])
        invalid = result["rows"][1]
        self.assertEqual((invalid["line"], invalid["status"]), (2, "invalid"))
        self.assertTrue(invalid["reason"])
        with self.assertRaises(ValueError):
            core.export_result(result, str(self.output_parent))
        exported = core.export_result(result, str(self.output_parent), copy_files=False)
        self.assertEqual(self.copies(exported), {})
        self.assertTrue(any(row["清单行号"] == "2" and row["匹配说明"] for row in self.report(exported)))
        corrected = core.scan(str(self.source), "^README$", mode="regex")
        self.assertTrue(corrected["complete"])
        self.assert_output(core.export_result(corrected, str(self.output_parent)), ["notes/README"])

    def test_cancelled_scan_and_export_report_actual_copies(self):
        cancelled = threading.Event()
        cancelled.set()
        incomplete = core.scan(str(self.source), ".", mode="regex", cancel_event=cancelled)
        self.assertFalse(incomplete["complete"])
        with self.assertRaises(ValueError):
            core.export_result(incomplete, str(self.output_parent))
        self.report(core.export_result(incomplete, str(self.output_parent), copy_files=False))
        result = core.scan(str(self.source), "pdf", mode="extension")
        self.choose(result["rows"][0])
        event = threading.Event()
        actual_copy = core._copy_exclusive
        completed = []

        def copy_then_cancel(candidate, root, output, cancel_event):
            value = actual_copy(candidate, root, output, cancel_event)
            completed.append(str(Path(candidate).relative_to(self.source)))
            event.set()
            return value

        with mock.patch.object(core, "_copy_exclusive", side_effect=copy_then_cancel):
            exported = core.export_result(result, str(self.output_parent), cancel_event=event)
        self.assertTrue(exported["cancelled"])
        self.assertEqual(exported["failed"], 0)
        self.assertEqual(len(completed), 1)
        self.assert_output(exported, completed)
        self.assertIn("已取消", "\n".join(str(row) for row in self.report(exported)))

    def test_selected_file_disappears_other_selections_still_copy(self):
        result = core.scan(str(self.source), "pdf", mode="extension")
        row = result["rows"][0]
        self.choose(row)
        disappeared = Path(row["candidates"][0])
        disappeared.unlink()
        expected = [str(Path(path).relative_to(self.source)) for path in row["candidates"] if Path(path) != disappeared]
        exported = core.export_result(result, str(self.output_parent))
        self.assertEqual(exported["failed"], 1)
        self.assert_output(exported, expected)
        report_text = "\n".join(str(value) for row in self.report(exported) for value in row.values())
        self.assertIn(str(disappeared), report_text)
        self.assertIn("复制失败", report_text)

    def test_exact_mode_keeps_ambiguous_single_choice_compatibility(self):
        result = core.scan(str(self.source), "财务_2026_01.pdf")
        row = result["rows"][0]
        self.assertEqual(row["status"], "ambiguous")
        chosen = str(self.source / self.fixture[0])
        row.update(status="selected", selected_candidate=chosen)
        self.assert_output(core.export_result(result, str(self.output_parent)), [self.fixture[0]])

    def test_report_only_preserves_selection_trace_without_copies(self):
        before = self.snapshot()
        result = core.scan(str(self.source), "pdf\ntxt", mode="extension")
        self.choose(result["rows"][0])
        exported = core.export_result(result, str(self.output_parent), copy_files=False)
        self.assert_output(exported, [])
        rows = [row for row in self.report(exported) if row["记录类型"] == "清单项"]
        self.assertEqual(set(rows[0]["实际选择路径"].splitlines()), set(result["rows"][0]["candidates"]))
        self.assertEqual(self.snapshot(), before)

    def test_large_real_file_rule_report_is_complete_and_excel_cell_safe(self):
        bulk = self.source / "bulk"
        bulk.mkdir()
        for number in range(500):
            (bulk / (f"report_{number:04d}_" + "long_name_" * 12 + ".txt")).write_bytes(b"synthetic")
        before = self.snapshot()
        result = core.scan(str(self.source), "txt", mode="extension")
        row = result["rows"][0]
        self.assertEqual(len(row["candidates"]), 501)
        self.choose(row)
        exported = core.export_result(result, str(self.output_parent), copy_files=False)
        report = self.report(exported)
        values = [str(value or "") for item in report for value in item.values()]
        self.assertLessEqual(max(map(len, values)), 32767)
        all_values = "\n".join(values)
        for candidate in row["candidates"]:
            self.assertIn(candidate, all_values)
        self.assertEqual(self.copies(exported), {})
        self.assertEqual(self.snapshot(), before)


class ProductUIFlowTests(unittest.TestCase):
    """Exercise an isolated withdrawn Tk window; never touch an open user window."""

    def setUp(self):
        import tkinter as tk
        import app
        self.app_module = app
        self.files = ProductFlowTests()
        self.files.setUp()
        self.root = tk.Tk()
        self.root.withdraw()
        self.ui = app.ListPickApp(self.root)
        self.dialogs = {}
        self.patchers = []
        for name in ("showerror", "showwarning", "showinfo", "askyesno"):
            patcher = mock.patch.object(app.messagebox, name, return_value=True)
            self.dialogs[name] = patcher.start()
            self.patchers.append(patcher)
        self.ui.source_var.set(str(self.files.source))
        self.ui.parent_var.set(str(self.files.output_parent))
        self.root.update()

    def tearDown(self):
        if self.ui.cancel_event:
            self.ui.cancel_event.set()
        for timer in self.root.tk.call("after", "info"):
            self.root.after_cancel(timer)
        self.root.destroy()
        for patcher in self.patchers:
            patcher.stop()
        self.files.tearDown()

    def wait_idle(self):
        deadline = time.monotonic() + 5
        while self.ui.busy_kind and time.monotonic() < deadline:
            self.root.update()
            time.sleep(.01)
        self.assertIsNone(self.ui.busy_kind, "Synthetic operation did not finish within 5 seconds")
        self.root.update()

    def scan_ui(self, text, mode="regex"):
        self.ui.mode_var.set(mode)
        self.ui._replace_list(text)
        self.root.update()
        self.ui._start_scan()
        self.wait_idle()
        return self.ui.result

    def select_overlapping_rules(self):
        result = self.scan_ui(r"^财务_2026_\d{2}\.pdf$" + "\n" + r"\.pdf$")
        self.assertTrue(result["complete"])
        for row in result["rows"]:
            self.assertTrue(self.ui._apply_candidates(row, list(row["candidates"])))
        self.assertEqual(len(self.ui._copy_plan()[0]), 4)
        return result

    def test_hidden_view_does_not_reduce_real_copy_plan_or_confirmation(self):
        self.select_overlapping_rules()
        self.assertEqual(self.ui.selection_count_var.get(), "计划 4 个文件 / 2 行")
        self.ui.search_var.set("不会匹配任何行的搜索词")
        self.assertEqual(self.ui.display_count_var.get(), "显示 0 / 2 行")
        self.assertEqual(len(self.ui._copy_plan()[0]), 4)
        self.assertEqual(str(self.ui.copy_button.cget("state")), "normal")
        self.ui._start_export(True)
        self.wait_idle()
        confirmation = self.dialogs["askyesno"].call_args.args[1]
        self.assertIn("4 个文件", confirmation)
        self.assertIn("2 条", confirmation)
        self.assertIn("不减少复制范围", confirmation)
        self.assertIn("复制 4 个", self.ui.status_var.get())
        output = Path(self.ui.last_output)
        actual = {str(path.relative_to(output)) for path in output.rglob("*")
                  if path.is_file() and path.name != "核对报告.csv"}
        self.assertEqual(actual, {str(Path(name)) for name in self.files.fixture[:4]})
        self.dialogs["showerror"].assert_not_called()

    def test_mode_and_scan_filters_invalidate_but_view_filters_do_not(self):
        setters = [
            lambda: self.ui.mode_label_var.set("文件后缀"),
            lambda: self.ui.include_var.set("pdf"),
            lambda: self.ui.exclude_var.set("txt"),
            lambda: self.ui._replace_list("README"),
        ]
        for setter in setters:
            with self.subTest(setter=setter):
                self.ui.include_var.set("")
                self.ui.exclude_var.set("")
                self.ui.search_var.set("")
                self.ui.filter_var.set("全部状态")
                self.select_overlapping_rules()
                self.ui.filter_var.set("未找到")
                self.assertFalse(self.ui.result_dirty)
                self.assertEqual(len(self.ui._copy_plan()[0]), 4)
                setter()
                self.root.update()
                self.assertTrue(self.ui.result_dirty)
                self.assertEqual(self.ui._copy_plan(), ([], 0))
                self.assertTrue(all("selected_candidates" not in row for row in self.ui.result["rows"]))
                self.assertEqual(str(self.ui.copy_button.cget("state")), "disabled")
                self.assertEqual(str(self.ui.report_button.cget("state")), "disabled")
                self.assertIn("重新扫描", self.ui.result_note_var.get())
                self.ui._start_export(True)
                self.assertEqual(list(self.files.output_parent.iterdir()), [])

    def test_clear_overlapping_rule_keeps_other_rules_files(self):
        result = self.select_overlapping_rules()
        self.ui.tree.selection_set("row-0")
        self.ui._clear_candidate()
        self.assertEqual(result["rows"][0]["status"], "multiple")
        self.assertEqual(self.ui.selection_count_var.get(), "计划 4 个文件 / 1 行")
        self.ui.tree.selection_set("row-1")
        self.ui._clear_candidate()
        self.assertEqual(self.ui.selection_count_var.get(), "计划 0 个文件 / 0 行")
        self.assertEqual(str(self.ui.copy_button.cget("state")), "disabled")

    def test_invalid_regex_and_bad_filter_recover_without_stale_export(self):
        bad = self.scan_ui("README\n(")
        self.assertFalse(bad["complete"])
        self.assertEqual(str(self.ui.copy_button.cget("state")), "disabled")
        self.assertEqual(str(self.ui.report_button.cget("state")), "normal")
        valid = self.scan_ui("^README$")
        self.assertTrue(valid["complete"])
        self.assertEqual(self.ui.selection_count_var.get(), "计划 1 个文件 / 1 行")
        self.ui.include_var.set(".tar.gz")
        self.ui._start_scan()
        self.wait_idle()
        self.assertTrue(self.ui.result_dirty)
        self.assertEqual(str(self.ui.copy_button.cget("state")), "disabled")
        self.dialogs["showerror"].assert_called_once()
        self.ui.include_var.set("")
        self.ui._start_scan()
        self.wait_idle()
        self.assertTrue(self.ui.result["complete"])
        self.assertEqual(self.ui.selection_count_var.get(), "计划 1 个文件 / 1 行")


if __name__ == "__main__":
    unittest.main(verbosity=2)
