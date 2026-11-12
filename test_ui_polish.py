"""Independent UI acceptance using only this process's Tk windows and fake files."""

import copy
from pathlib import Path
import tempfile
import time
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk
import unittest
from unittest import mock

import app


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


class UIPolishTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="list-pick-ui-polish-")
        self.base = Path(self.temp.name)
        self.source = self.base / "source"
        self.exports = self.base / "exports"
        self.source.mkdir()
        self.exports.mkdir()
        for number in range(12):
            (self.source / f"invoice_{number:03d}.txt").write_text(f"synthetic {number}", encoding="utf-8")
        for folder in ("first", "second"):
            (self.source / folder).mkdir()
            (self.source / folder / "same.txt").write_text(folder, encoding="utf-8")
        self.patchers = []
        self.dialog_messages = {}
        for name in ("showerror", "showwarning", "showinfo", "askyesno"):
            patcher = mock.patch.object(app.messagebox, name, return_value=True)
            self.dialog_messages[name] = patcher.start()
            self.patchers.append(patcher)
        self.create_ui()

    def create_ui(self, scaling=None):
        self.root = tk.Tk()
        self.root.withdraw()
        if scaling is not None:
            self.root.tk.call("tk", "scaling", scaling)
        self.ui = app.ListPickApp(self.root)
        # No OS input or window operations. This is a new, offscreen Tk window.
        self.root.geometry("1100x680+30000+30000")
        self.root.deiconify()
        self.ui.source_var.set(str(self.source))
        self.ui.parent_var.set(str(self.exports))
        self.root.update()

    def destroy_ui(self):
        if self.ui.cancel_event:
            self.ui.cancel_event.set()
        for timer in self.root.tk.call("after", "info"):
            self.root.after_cancel(timer)
        self.root.destroy()

    def tearDown(self):
        self.destroy_ui()
        for patcher in self.patchers:
            patcher.stop()
        self.temp.cleanup()

    def wait_idle(self):
        deadline = time.monotonic() + 5
        while self.ui.busy_kind and time.monotonic() < deadline:
            self.root.update()
            time.sleep(.01)
        self.assertIsNone(self.ui.busy_kind)
        self.root.update()

    def scan(self, text=r"^invoice_\d{3}\.txt$", mode="regex"):
        self.ui.mode_var.set(mode)
        self.ui._replace_list(text)
        self.root.update()
        self.ui._start_scan()
        self.wait_idle()
        self.assertTrue(self.ui.result["complete"])
        self.ui.tree.selection_set("row-0")
        self.ui._show_selection()
        return self.ui.result["rows"][0]

    def candidate_dialog(self, action):
        errors = []
        ran = []

        def perform():
            dialogs = [child for child in self.root.winfo_children() if isinstance(child, tk.Toplevel)]
            if len(dialogs) != 1:
                errors.append(AssertionError(f"Expected exactly one own dialog, got {len(dialogs)}"))
                return
            dialog = dialogs[0]
            try:
                dialog.update_idletasks()
                action(dialog)
                ran.append(True)
            except Exception as exc:
                errors.append(exc)
            finally:
                if dialog.winfo_exists():
                    dialog.destroy()

        timer = self.root.after(100, perform)
        watchdog = self.root.after(5000, lambda: [child.destroy() for child in self.root.winfo_children() if isinstance(child, tk.Toplevel)])
        self.ui._choose_candidate()
        for identifier in (timer, watchdog):
            try:
                self.root.after_cancel(identifier)
            except tk.TclError:
                pass
        if errors:
            raise errors[0]
        self.assertTrue(ran, "Candidate dialog did not execute its interaction")

    def search(self, dialog, text):
        entries = [widget for widget in descendants(dialog) if isinstance(widget, ttk.Entry)]
        self.assertEqual(len(entries), 1)
        entries[0].delete(0, "end")
        entries[0].insert(0, text)
        dialog.update()
        trees = [widget for widget in descendants(dialog) if isinstance(widget, ttk.Treeview)]
        self.assertEqual(len(trees), 1)
        return trees[0]

    def toggle_first(self, tree):
        children = tree.get_children()
        self.assertTrue(children)
        bounds = tree.bbox(children[0], "#0")
        self.assertTrue(bounds, "First checkbox is not visible")
        x, y, width, height = bounds
        tree.event_generate("<Button-1>", x=x + width // 2, y=y + height // 2)
        tree.event_generate("<ButtonRelease-1>", x=x + width // 2, y=y + height // 2)
        tree.update()

    def button(self, dialog, prefix):
        buttons = [widget for widget in descendants(dialog) if isinstance(widget, ttk.Button)
                   and str(widget.cget("text")).startswith(prefix)]
        self.assertEqual(len(buttons), 1, f"Missing or ambiguous dialog action: {prefix}")
        return buttons[0]

    def test_search_009_check_and_real_copy_only_that_file(self):
        row = self.scan()

        def interact(dialog):
            tree = self.search(dialog, "009")
            self.assertEqual(len(tree.get_children()), 1)
            x, y, width, height = tree.bbox(tree.get_children()[0], "#1")
            tree.event_generate("<Button-1>", x=x + min(20, width // 2), y=y + height // 2)
            tree.event_generate("<ButtonRelease-1>", x=x + min(20, width // 2), y=y + height // 2)
            tree.update()
            self.assertFalse(dialog.candidate_selected_paths, "Highlighting a path must not check it")
            self.toggle_first(tree)
            self.button(dialog, "确认").invoke()

        self.candidate_dialog(interact)
        expected = str(self.source / "invoice_009.txt")
        self.assertEqual(self.ui._row_selected_paths(row), [expected])
        self.ui._start_export(True)
        self.wait_idle()
        files = list(Path(self.ui.last_output).rglob("*.txt"))
        self.assertEqual([path.name for path in files], ["invoice_009.txt"])
        self.assertEqual(files[0].read_text(encoding="utf-8"), "synthetic 9")
        self.assertIn("复制 1 个", self.ui.status_var.get())

    def test_selections_survive_search_changes(self):
        row = self.scan()

        def interact(dialog):
            self.toggle_first(self.search(dialog, "009"))
            self.toggle_first(self.search(dialog, "010"))
            self.search(dialog, "not-found-anywhere")
            self.button(dialog, "确认").invoke()

        self.candidate_dialog(interact)
        self.assertEqual({Path(path).name for path in self.ui._row_selected_paths(row)},
                         {"invoice_009.txt", "invoice_010.txt"})

    def test_cancel_keeps_previous_selection_unchanged(self):
        row = self.scan()
        self.ui._apply_candidates(row, [str(self.source / "invoice_009.txt")])
        previous = copy.deepcopy(row)

        def interact(dialog):
            self.toggle_first(self.search(dialog, "010"))
            self.button(dialog, "取消，不更改").invoke()

        self.candidate_dialog(interact)
        self.assertEqual(row, previous)

    def test_bulk_actions_only_change_current_search_results(self):
        row = self.scan()

        def interact(dialog):
            self.search(dialog, "009")
            self.button(dialog, "勾选当前结果").invoke()
            self.search(dialog, "010")
            self.button(dialog, "勾选当前结果").invoke()
            self.search(dialog, "009")
            self.button(dialog, "取消当前结果").invoke()
            self.button(dialog, "确认").invoke()

        self.candidate_dialog(interact)
        self.assertEqual(self.ui._row_selected_paths(row), [str(self.source / "invoice_010.txt")])

    def test_confirm_empty_selection_clears_previously_selected_rule(self):
        cases = [
            ("regex", r"^invoice_\d{3}\.txt$", "invoice_009.txt", "009", "multiple"),
            ("full", "same.txt", "first/same.txt", "first", "ambiguous"),
        ]
        for mode, query, selected, search, restored in cases:
            with self.subTest(mode=mode):
                row = self.scan(query, mode=mode)
                self.assertTrue(self.ui._apply_candidates(row, [str(self.source / selected)]))

                def interact(dialog):
                    self.toggle_first(self.search(dialog, search))
                    button = self.button(dialog, "确认取消本行选择")
                    self.assertEqual(str(button.cget("state")), "normal")
                    button.invoke()

                self.candidate_dialog(interact)
                self.assertEqual(row["status"], restored)
                self.assertEqual(self.ui._row_selected_paths(row), [])
                self.assertNotIn("selected_candidates", row)
                self.assertNotIn("selected_candidate", row)
                self.assertEqual(str(self.ui.copy_button.cget("state")), "disabled")

    def test_exact_name_dialog_allows_only_one_checked_file(self):
        row = self.scan("same.txt", mode="full")

        def interact(dialog):
            self.toggle_first(self.search(dialog, "first"))
            self.toggle_first(self.search(dialog, "second"))
            buttons = [widget for widget in descendants(dialog) if isinstance(widget, ttk.Button)
                       and str(widget.cget("text")) in ("确认使用所选", "确认使用", "使用此文件")]
            self.assertEqual(len(buttons), 1)
            buttons[0].invoke()

        self.candidate_dialog(interact)
        self.assertEqual(self.ui._row_selected_paths(row), [str(self.source / "second/same.txt")])

    def test_tab_and_shift_tab_navigate_without_inserting_tab(self):
        self.ui._replace_list("invoice_009.txt")
        self.root.update()
        self.ui.list_text.focus_force()
        self.root.update()
        self.assertEqual(self.root.focus_get(), self.ui.list_text)
        self.ui.list_text.event_generate("<Tab>")
        self.root.update()
        self.assertEqual(self.ui.list_text.get("1.0", "end-1c"), "invoice_009.txt")
        following = self.root.focus_get()
        self.assertIsNotNone(following)
        self.assertNotEqual(following, self.ui.list_text)
        following.event_generate("<Shift-Tab>")
        self.root.update()
        self.assertEqual(self.root.focus_get(), self.ui.list_text)
        self.assertNotIn("\t", self.ui.list_text.get("1.0", "end-1c"))

    def test_utf16_file_import_through_gui_action(self):
        path = self.base / "utf16-list.txt"
        path.write_text("invoice_009.txt\ninvoice_010.txt", encoding="utf-16")
        with mock.patch.object(app.filedialog, "askopenfilename", return_value=str(path)):
            self.ui._import_list()
        self.root.update()
        self.assertEqual(self.ui.list_text.get("1.0", "end-1c").splitlines(), ["invoice_009.txt", "invoice_010.txt"])
        self.assertEqual(self.ui.list_count_var.get(), "2 行")
        self.ui.mode_var.set("full")
        self.ui._start_scan()
        self.wait_idle()
        self.assertEqual([row["status"] for row in self.ui.result["rows"]], ["unique", "unique"])
        self.dialog_messages["showerror"].assert_not_called()

    def test_high_dpi_minimum_window_keeps_results_and_actions_usable(self):
        for scaling in (2.0, 2.6666667):
            with self.subTest(scaling=scaling):
                self.destroy_ui()
                self.create_ui(scaling=scaling)
                self.scan("invoice_001.txt\ninvoice_002.txt\ninvoice_003.txt", mode="full")
                minimum_width, minimum_height = self.root.minsize()
                self.root.geometry(f"{minimum_width}x{minimum_height}+30000+30000")
                self.root.update()
                root_x, root_y = self.root.winfo_rootx(), self.root.winfo_rooty()
                width, height = self.root.winfo_width(), self.root.winfo_height()
                for widget in (self.ui.scan_button, self.ui.copy_button, self.ui.report_button, self.ui.tree):
                    x, y = widget.winfo_rootx() - root_x, widget.winfo_rooty() - root_y
                    self.assertGreaterEqual(x, 0)
                    self.assertGreaterEqual(y, 0)
                    self.assertLessEqual(x + widget.winfo_width(), width)
                    self.assertLessEqual(y + widget.winfo_height(), height)
                style = ttk.Style(self.root)
                row_height = int(style.lookup("Treeview", "rowheight"))
                row_font = tkfont.Font(root=self.root, font=style.lookup("Treeview", "font"))
                self.assertGreaterEqual(row_height, row_font.metrics("linespace"), "Result row clips text at this DPI")
                self.assertGreaterEqual(self.ui.tree.winfo_height(), row_height * 3)
                self.assertTrue(self.ui.tree.bbox("row-0", "query"))
                for widget in (self.ui.scan_button, self.ui.copy_button):
                    self.assertGreaterEqual(widget.winfo_width(), widget.winfo_reqwidth())


if __name__ == "__main__":
    unittest.main(verbosity=2)
