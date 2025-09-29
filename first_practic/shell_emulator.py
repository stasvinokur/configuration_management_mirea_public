import argparse
import base64
import fnmatch
import getpass
import shlex
import socket
import sys
import tkinter as tk
from pathlib import Path
from tkinter import scrolledtext
import xml.etree.ElementTree as ET


# =========================
# Простая VFS (в памяти)
# =========================

class VNode:
    def __init__(self, name: str, parent: 'VDir | None'):
        self.name = name
        self.parent = parent

    def abspath(self) -> str:
        # Корень
        if self.parent is None:
            return "/"
        parts = []
        cur: VNode | None = self
        while cur and cur.parent is not None:
            parts.append(cur.name)
            cur = cur.parent
        return "/" + "/".join(reversed(parts))


class VFile(VNode):
    def __init__(self, name: str, content: bytes, parent: 'VDir'):
        super().__init__(name, parent)
        self.content = content


class VDir(VNode):
    def __init__(self, name: str, parent: 'VDir | None' = None):
        super().__init__(name, parent)
        self.children: dict[str, VNode] = {}

    def get(self, name: str) -> 'VNode | None':
        return self.children.get(name)

    def add_dir(self, name: str) -> 'VDir':
        if name in self.children and not isinstance(self.children[name], VDir):
            raise ValueError(f"в VFS уже есть файл с именем '{name}'")
        d = self.children.get(name)
        if isinstance(d, VDir):
            return d
        d = VDir(name, self)
        self.children[name] = d
        return d

    def add_file(self, name: str, content: bytes) -> 'VFile':
        if name in self.children and not isinstance(self.children[name], VFile):
            raise ValueError(f"в VFS уже есть каталог с именем '{name}'")
        f = VFile(name, content, self)
        self.children[name] = f
        return f

    def list_names(self) -> list[str]:
        names = []
        for n, node in self.children.items():
            if isinstance(node, VDir):
                names.append(n + "/")
            else:
                names.append(n)
        names.sort(key=lambda s: s.lower())
        return names


def _import_physical_dir_to_vfs(dir_path: Path) -> VDir:
    """
    Служебный режим совместимости: импорт из реального каталога
    в ОЗУ (ничего на диске не меняем).
    """
    root = VDir("/")
    def walk(host_dir: Path, vdir: VDir):
        for entry in sorted(host_dir.iterdir(), key=lambda p: p.name.lower()):
            if entry.is_dir():
                child = vdir.add_dir(entry.name)
                walk(entry, child)
            else:
                try:
                    data = entry.read_bytes()
                except Exception:
                    data = b""
                vdir.add_file(entry.name, data)
    walk(dir_path, root)
    return root


def load_vfs_from_xml(xml_path: Path) -> VDir:
    """
    Загружает VFS из XML.
    Поддерживается структура:
    <vfs>
      <dir name="/">
        <dir name="etc">
          <file name="motd" encoding="utf-8">Привет</file>
        </dir>
        <file name="bin" base64="true">AAECAw==</file>
      </dir>
    </vfs>
    """
    if not xml_path.exists():
        raise FileNotFoundError(f"файл '{xml_path}' не найден")

    try:
        tree = ET.parse(xml_path)
    except ET.ParseError as e:
        raise ValueError(f"некорректный XML: {e}") from e

    root_el = tree.getroot()
    if root_el.tag != "vfs":
        raise ValueError("ожидался корневой тег <vfs>")

    dirs = [el for el in root_el.findall("dir") if el.get("name") == "/"]
    if not dirs:
        raise ValueError("в <vfs> отсутствует <dir name=\"/\">")
    top_el = dirs[0]

    root = VDir("/")
    _build_dir_from_xml(top_el, root)
    return root


def _build_dir_from_xml(dir_el: ET.Element, vdir: VDir) -> None:
    for child in dir_el:
        if child.tag == "dir":
            name = child.get("name")
            if not name:
                raise ValueError("в <dir> отсутствует атрибут 'name'")
            new_dir = vdir.add_dir(name)
            _build_dir_from_xml(child, new_dir)
        elif child.tag == "file":
            name = child.get("name")
            if not name:
                raise ValueError("в <file> отсутствует атрибут 'name'")
            is_b64 = (child.get("base64", "false").lower() in ("1", "true", "yes"))
            encoding = child.get("encoding")
            text = child.text or ""
            try:
                if is_b64:
                    data = base64.b64decode(text.encode("ascii"), validate=True)
                else:
                    if encoding:
                        data = text.encode(encoding)
                    else:
                        data = text.encode("utf-8")
            except Exception as e:
                raise ValueError(f"ошибка чтения содержимого файла '{name}': {e}") from e
            vdir.add_file(name, data)
        else:
            # неизвестные теги просто игнорируем, чтобы не падать
            continue


def _resolve_in_vfs(start: VNode, a_root: VDir, path_str: str | None) -> VNode:
    """
    Разрешение путей внутри VFS. Поддерживает:
    - абсолютные пути (/a/b)
    - относительные пути
    - . и ..
    """
    if path_str is None or path_str == ".":
        return start

    cur: VNode
    if path_str.startswith("/"):
        cur = a_root
        parts = [p for p in path_str.split("/") if p]
    else:
        cur = start
        parts = [p for p in path_str.split("/") if p]

    for part in parts:
        if part == ".":
            continue
        if part == "..":
            if cur.parent is None:
                cur = a_root
            else:
                cur = cur.parent
            continue

        if not isinstance(cur, VDir):
            raise ValueError(f"не каталог: {cur.abspath()}")
        nxt = cur.get(part)
        if nxt is None:
            raise FileNotFoundError(f"нет такого файла или каталога: {part}")
        cur = nxt

    return cur


def _walk_vfs(start: VNode, *, maxdepth: int | None = None):
    """
    Генератор обхода VFS в глубину (DFS).
    depth=0 для стартового узла; ограничение maxdepth включительно.
    """
    stack: list[tuple[VNode, int]] = [(start, 0)]
    while stack:
        node, depth = stack.pop()
        yield node, depth
        if isinstance(node, VDir):
            if (maxdepth is None) or (depth < maxdepth):
                for name in sorted(node.children.keys(), key=str.lower, reverse=True):
                    stack.append((node.children[name], depth + 1))


def _resolve_parent_for_creation(start: VNode, a_root: VDir, path_str: str) -> tuple[VDir, str]:
    """
    Возвращает (родительский каталог, имя создаваемого узла).
    Не создаёт каталоги автоматически.
    """
    if not path_str:
        raise ValueError("пустой путь")
    # Уберём лишние слеши в конце (кроме корня)
    if path_str != "/":
        path_str = path_str.rstrip("/")
    if path_str in ("", "/"):
        raise ValueError("нельзя создавать объект с именем '/'")

    # Выделяем имя и путь к родителю
    if path_str.startswith("/"):
        cur: VNode = a_root
        parts = [p for p in path_str.split("/") if p]
    else:
        cur = start
        parts = [p for p in path_str.split("/") if p]

    if not parts:
        raise ValueError("неверный путь")

    name = parts[-1]
    if name in (".", ".."):
        raise ValueError("некорректное имя файла/каталога")

    dir_parts = parts[:-1]
    for part in dir_parts:
        if part == ".":
            continue
        if part == "..":
            if cur.parent is None:
                cur = a_root
            else:
                cur = cur.parent
            continue
        if not isinstance(cur, VDir):
            raise ValueError(f"не каталог: {cur.abspath()}")
        nxt = cur.get(part)
        if nxt is None:
            raise FileNotFoundError(f"нет такого каталога: {part}")
        if not isinstance(nxt, VDir):
            raise ValueError(f"ожидался каталог, а найден файл: {part}")
        cur = nxt

    if not isinstance(cur, VDir):
        raise ValueError(f"не каталог: {cur.abspath()}")
    return cur, name


def _copy_recursive(src: VNode, dst_parent: VDir, new_name: str) -> None:
    """
    Рекурсивно копирует src в dst_parent/new_name.
    Если целевой узел уже существует:
      - файл перезаписывается;
      - каталог — ошибка (во избежание слияния).
    """
    # Проверим наличие цели
    existing = dst_parent.get(new_name)
    if isinstance(src, VFile):
        # Разрешаем перезапись файла
        dst_parent.add_file(new_name, src.content)
        return
    if isinstance(src, VDir):
        if existing is not None:
            # Не позволяем "сливать" каталоги, чтобы логика была простой и предсказуемой
            raise ValueError(f"цель уже существует: {dst_parent.abspath().rstrip('/')}/{new_name}")
        new_dir = dst_parent.add_dir(new_name)
        # Копируем содержимое
        for child_name, child in sorted(src.children.items(), key=lambda kv: kv[0].lower()):
            if isinstance(child, VFile):
                new_dir.add_file(child_name, child.content)
            else:
                _copy_recursive(child, new_dir, child_name)
        return
    raise ValueError("неизвестный тип узла источника")


# =========================
# GUI-эмулятор оболочки
# =========================

class ShellEmulator(tk.Tk):
    def __init__(self, vfs_source: Path | None, script_path: Path | None, raw_args: dict):
        super().__init__()

        # ---- ОС-атрибуты и окно ----
        self.username = getpass.getuser()
        self.hostname = socket.gethostname()
        self.title(f"Эмулятор - [{self.username}@{self.hostname}]")
        self.geometry("900x520")
        self.minsize(600, 360)
        self.configure(padx=8, pady=8)

        # ---- область вывода + ввод ----
        self.out = scrolledtext.ScrolledText(
            self, wrap=tk.WORD, state="disabled", font=("JetBrains Mono", 11)
        )
        self.out.pack(fill=tk.BOTH, expand=True)

        self.inp = tk.Entry(self, font=("JetBrains Mono", 11))
        self.inp.pack(fill=tk.X, pady=(8, 0))
        self.inp.focus_set()

        # ---- привязки ----
        self.inp.bind("<Return>", self.on_enter)
        self.inp.bind("<Up>", self.on_history_up)
        self.inp.bind("<Down>", self.on_history_down)
        self.bind("<Control-w>", lambda e: self.destroy())

        # ---- состояние REPL ----
        self.prompt = f"[{self.username}@{self.hostname}]$ "
        self.history: list[str] = []
        self.hist_idx: int | None = None

        # ---- VFS (в памяти) ----
        self.vfs_root: VDir
        self.cwd: VDir
        self.writeln("--- Параметры запуска ---")
        self.writeln(f"VFS      : {str(vfs_source) if vfs_source else '<не задан>'}")
        self.writeln(f"Script   : {str(script_path) if script_path else '<не задан>'}")
        self.writeln(f"argv     : {raw_args}")
        self.writeln("-------------------------")

        self._init_vfs(vfs_source)

        self.write(self.prompt)

        # Если есть стартовый скрипт — выполнить после старта окна
        if script_path:
            self.after(50, lambda: self._run_script_with_ui(script_path))

    # ---------- сервис вывода ----------
    def write(self, text: str):
        self.out.configure(state="normal")
        self.out.insert(tk.END, text)
        self.out.see(tk.END)
        self.out.configure(state="disabled")

    def writeln(self, text: str = ""):
        self.write(text + "\n")

    # ---------- история ----------
    def on_history_up(self, _event=None):
        if not self.history:
            return "break"
        if self.hist_idx is None:
            self.hist_idx = len(self.history) - 1
        else:
            self.hist_idx = max(0, self.hist_idx - 1)
        self._set_input(self.history[self.hist_idx])
        return "break"

    def on_history_down(self, _event=None):
        if not self.history or self.hist_idx is None:
            return "break"
        self.hist_idx = min(len(self.history) - 1, self.hist_idx + 1)
        self._set_input(self.history[self.hist_idx])
        if self.hist_idx == len(self.history) - 1:
            self.hist_idx = None
        return "break"

    def _set_input(self, text: str):
        self.inp.delete(0, tk.END)
        self.inp.insert(0, text)
        self.inp.icursor(tk.END)

    # ---------- инициализация VFS ----------
    def _init_vfs(self, vfs_source: Path | None):
        """
        Логика этапа 3:
        - Источник VFS — XML-файл (основной режим).
        - Сообщаем об ошибке загрузки (нет файла / формат неверный).
        - Совместимость: если передали каталог, импортируем его в память.
        - Если ничего не передали или была ошибка — создаём минимальную VFS по умолчанию.
        """
        if vfs_source:
            p = Path(vfs_source)
            try:
                if p.is_file() and p.suffix.lower() == ".xml":
                    self.vfs_root = load_vfs_from_xml(p)
                    self.writeln(f"VFS: загружено из XML: {p}")
                elif p.is_dir():
                    self.vfs_root = _import_physical_dir_to_vfs(p)
                    self.writeln(f"VFS: импортировано из каталога (в память): {p}")
                else:
                    raise FileNotFoundError(f"'{p}' не найдено как файл .xml или каталог")
            except (FileNotFoundError, ValueError) as e:
                self.writeln(f"Ошибка загрузки VFS: {e}")
                self.writeln("Продолжаю с минимальной VFS по умолчанию.")
                self.vfs_root = self._default_vfs()
        else:
            self.vfs_root = self._default_vfs()

        self.cwd = self.vfs_root  # начинаем в корне

    def _default_vfs(self) -> VDir:
        root = VDir("/")
        etc = root.add_dir("etc")
        home = root.add_dir("home")
        user = home.add_dir(self.username or "user")
        root.add_file("readme.txt", b"This is VFS")
        etc.add_file("motd", "Добро пожаловать в учебный эмулятор!".encode("utf-8"))
        user.add_file("notes.txt", "Привет из VFS!\n".encode("utf-8"))
        return root

    # ---------- обработка строки ----------
    def on_enter(self, _event=None):
        line = self.inp.get()
        self.inp.delete(0, tk.END)
        # эхо команды как в терминале
        self.writeln(line)
        if line.strip():
            self.history.append(line)
        self.hist_idx = None

        ok = self.process_line(line)
        # новый промпт
        if self.winfo_exists():
            self.write(self.prompt)
        return ok

    def process_line(self, line: str, *, echo: bool = False) -> bool:
        try:
            argv = shlex.split(line, posix=True)
        except ValueError as e:
            self.writeln(f"ошибка парсинга: {e}")
            return False

        if not argv:
            return True

        cmd, *args = argv
        try:
            return self.dispatch(cmd, args)
        except Exception as e:
            self.writeln(f"ошибка выполнения: {e!r}")
            return False

    # ---------- запуск стартового скрипта ----------
    def _run_script_with_ui(self, script_path: Path):
        sp = Path(script_path)
        if not sp.exists() or not sp.is_file():
            self.writeln(f"Ошибка: скрипт '{sp}' не найден.")
            self.write(self.prompt)
            return

        try:
            text = sp.read_text(encoding="utf-8")
        except Exception as e:
            self.writeln(f"Ошибка чтения скрипта: {e!r}")
            self.write(self.prompt)
            return

        stop = False
        for lineno, raw in enumerate(text.splitlines(), start=1):
            line = raw.rstrip("\n")
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue

            self.writeln(f"{self.prompt}{line}")

            ok = self.process_line(line)
            if not ok:
                self.writeln(f"Остановка скрипта на строке {lineno}.")
                stop = True
                break

        if not stop:
            self.writeln("(скрипт завершён)")

        if self.winfo_exists():
            self.write(self.prompt)

    # ---------- команды ----------
    def dispatch(self, cmd: str, args: list[str]) -> bool:
        if cmd == "exit":
            self.writeln("Выход...")
            self.after(50, self.destroy)
            return True

        if cmd == "help":
            self.writeln(
                "Доступные команды: help, ls [path], cd [path], pwd, echo ..., "
                "find [path] [-name PATTERN] [-type f|d] [-maxdepth N], "
                "uname [-asnrmpo], touch FILE..., cp [-r] SRC DST, exit"
            )
            return True

        if cmd == "pwd":
            self.writeln(self.cwd.abspath())
            return True

        if cmd == "echo":
            self.writeln(" ".join(args))
            return True

        if cmd == "uname":
            # Минимальная эмуляция uname
            flags = [a for a in args if a.startswith("-")]
            if not flags:
                self.writeln("VFS-Emu")
                return True
            if "-a" in flags:
                self.writeln(f"VFS-Emu {self.hostname} 0.1 x86_64 GNU/Linux")
                return True
            out: list[str] = []
            for fl in flags:
                if fl == "-s":
                    out.append("VFS-Emu")
                elif fl == "-n":
                    out.append(self.hostname)
                elif fl == "-r":
                    out.append("0.1")
                elif fl in ("-m", "-p"):
                    out.append("x86_64")
                elif fl == "-o":
                    out.append("GNU/Linux")
                else:
                    self.writeln(f"uname: неизвестная опция {fl}")
                    return False
            self.writeln(" ".join(out))
            return True

        if cmd == "ls":
            path = args[0] if args else "."
            try:
                target = _resolve_in_vfs(self.cwd, self.vfs_root, path)
            except (FileNotFoundError, ValueError) as e:
                self.writeln(f"ls: {e}")
                return False

            if isinstance(target, VFile):
                self.writeln(target.name)
                return True

            names = target.list_names()
            self.writeln("  ".join(names))
            return True

        if cmd == "find":
            # find [path] [-name PATTERN] [-type f|d] [-maxdepth N]
            path = "."
            name_pat: str | None = None
            type_filter: str | None = None  # 'f' или 'd'
            maxdepth: int | None = None

            i = 0
            while i < len(args):
                a = args[i]
                if not a.startswith("-") and path == ".":
                    path = a
                    i += 1
                    continue
                if a == "-name" and i + 1 < len(args):
                    name_pat = args[i + 1]
                    i += 2
                    continue
                if a == "-type" and i + 1 < len(args):
                    val = args[i + 1]
                    if val not in ("f", "d"):
                        self.writeln("find: -type ожидает f или d")
                        return False
                    type_filter = val
                    i += 2
                    continue
                if a == "-maxdepth" and i + 1 < len(args):
                    try:
                        maxdepth = int(args[i + 1])
                        if maxdepth < 0:
                            raise ValueError
                    except ValueError:
                        self.writeln("find: -maxdepth ожидает неотрицательное целое")
                        return False
                    i += 2
                    continue
                self.writeln(f"find: неизвестная опция или аргумент '{a}'")
                return False

            try:
                start_node = _resolve_in_vfs(self.cwd, self.vfs_root, path)
            except (FileNotFoundError, ValueError) as e:
                self.writeln(f"find: {e}")
                return False

            def match(node: VNode) -> bool:
                if type_filter == "f" and not isinstance(node, VFile):
                    return False
                if type_filter == "d" and not isinstance(node, VDir):
                    return False
                if name_pat is not None and not fnmatch.fnmatch(node.name, name_pat):
                    return False
                return True

            for node, depth in _walk_vfs(start_node, maxdepth=maxdepth):
                if match(node):
                    self.writeln(node.abspath())
            return True

        if cmd == "cd":
            path = args[0] if args else "/"
            try:
                target = _resolve_in_vfs(self.cwd, self.vfs_root, path)
            except (FileNotFoundError, ValueError) as e:
                self.writeln(f"cd: {e}")
                return False
            if not isinstance(target, VDir):
                self.writeln(f"cd: не каталог: {path}")
                return False
            self.cwd = target
            return True

        if cmd == "touch":
            if not args:
                self.writeln("usage: touch FILE...")
                return False
            ok_all = True
            for p in args:
                try:
                    node = _resolve_in_vfs(self.cwd, self.vfs_root, p)
                except (FileNotFoundError, ValueError):
                    # Создаём новый пустой файл
                    try:
                        parent, name = _resolve_parent_for_creation(self.cwd, self.vfs_root, p)
                        parent.add_file(name, b"")
                    except (FileNotFoundError, ValueError) as e:
                        self.writeln(f"touch: {e}")
                        ok_all = False
                else:
                    if isinstance(node, VDir):
                        self.writeln(f"touch: нельзя применить к каталогу: {p}")
                        ok_all = False
                    # файл уже существует — ничего не делаем
            return ok_all

        if cmd == "cp":
            recursive = False
            rest = []
            for a in args:
                if a == "-r":
                    recursive = True
                else:
                    rest.append(a)
            if len(rest) != 2:
                self.writeln("usage: cp [-r] SRC DST")
                return False
            src_path, dst_path = rest
            # Разрешаем источник
            try:
                src_node = _resolve_in_vfs(self.cwd, self.vfs_root, src_path)
            except (FileNotFoundError, ValueError) as e:
                self.writeln(f"cp: источник не найден: {e}")
                return False

            if isinstance(src_node, VDir) and not recursive:
                self.writeln("cp: для копирования каталогов используйте -r")
                return False

            # Пытаемся понять, существует ли назначение
            try:
                dst_node = _resolve_in_vfs(self.cwd, self.vfs_root, dst_path)
                dst_exists = True
            except (FileNotFoundError, ValueError):
                dst_node = None
                dst_exists = False

            try:
                if dst_exists:
                    if isinstance(dst_node, VFile):
                        if isinstance(src_node, VDir):
                            self.writeln("cp: нельзя копировать каталог в файл")
                            return False
                        # Перезапись файла
                        dst_node.content = src_node.content  # type: ignore[attr-defined]
                        return True
                    else:
                        # dst — каталог: копируем внутрь под исходным именем
                        target_parent = dst_node  # type: ignore[assignment]
                        new_name = src_node.name
                        # Защитимся от нежелательного слияния каталогов
                        if isinstance(src_node, VDir) and target_parent.get(new_name) is not None:
                            self.writeln(f"cp: цель уже существует: {target_parent.abspath().rstrip('/')}/{new_name}")
                            return False
                        _copy_recursive(src_node, target_parent, new_name)
                        return True
                else:
                    # Назначение не существует — создаём по указанному пути
                    parent, name = _resolve_parent_for_creation(self.cwd, self.vfs_root, dst_path)
                    if isinstance(src_node, VDir) and parent.get(name) is not None:
                        self.writeln(f"cp: цель уже существует: {parent.abspath().rstrip('/')}/{name}")
                        return False
                    _copy_recursive(src_node, parent, name)
                    return True
            except (FileNotFoundError, ValueError) as e:
                self.writeln(f"cp: {e}")
                return False

        # неизвестная команда — считаем ошибкой (важно для остановки скрипта)
        self.writeln(f"{cmd}: команда не найдена")
        return False


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="emulator",
        description="GUI-эмулятор оболочки с поддержкой VFS (XML) и стартовых скриптов",
    )
    parser.add_argument("--vfs", type=str, help="Путь к VFS: XML-файл (основной режим) или каталог (служебный)")
    parser.add_argument("--script", type=str, help="Путь к стартовому скрипту")
    return parser.parse_args(argv)


def main():
    ns = parse_args(sys.argv[1:])
    vfs_source = Path(ns.vfs).resolve() if ns.vfs else None
    script = Path(ns.script).resolve() if ns.script else None
    app = ShellEmulator(vfs_source, script, raw_args={"vfs": ns.vfs, "script": ns.script})
    app.mainloop()


if __name__ == "__main__":
    main()
