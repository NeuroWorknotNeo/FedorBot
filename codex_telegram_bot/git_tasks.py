"""Git-обвязка для задач из Telegram: ветка под задачу и промпт для Codex."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


class GitError(RuntimeError):
    """Команда git завершилась с ошибкой или репозиторий не готов к задаче."""


_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya",
}


async def run_cmd(
    args: list[str], cwd: Path, timeout: float = 120, env: Optional[dict[str, str]] = None
) -> tuple[int, str]:
    """Запускает команду, возвращает (код, объединённый stdout+stderr)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(cwd),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        return 127, f"{args[0]}: не найден ({exc})"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        return 124, f"Команда не завершилась за {int(timeout)} с и была прервана"
    return proc.returncode or 0, out.decode("utf-8", "replace")


async def git(args: list[str], cwd: Path, timeout: float = 120) -> str:
    code, out = await run_cmd(["git", *args], cwd, timeout=timeout)
    if code != 0:
        raise GitError(f"git {' '.join(args)} завершился с кодом {code}:\n{out.strip()[-1500:]}")
    return out.strip()


async def is_git_repo(path: Path) -> bool:
    if not path.is_dir():
        return False
    code, out = await run_cmd(["git", "rev-parse", "--is-inside-work-tree"], path, timeout=30)
    return code == 0 and out.strip() == "true"


async def current_branch(repo: Path) -> Optional[str]:
    code, out = await run_cmd(["git", "rev-parse", "--abbrev-ref", "HEAD"], repo, timeout=30)
    return out.strip() if code == 0 else None


async def has_remote(repo: Path, name: str = "origin") -> bool:
    code, out = await run_cmd(["git", "remote"], repo, timeout=30)
    return code == 0 and name in out.split()


async def remote_url(repo: Path, name: str = "origin") -> Optional[str]:
    code, out = await run_cmd(["git", "remote", "get-url", name], repo, timeout=30)
    return out.strip() if code == 0 and out.strip() else None


async def default_branch(repo: Path) -> str:
    """Определяет основную ветку: origin/HEAD → main → master → текущая."""
    code, out = await run_cmd(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], repo, timeout=30)
    if code == 0 and out.strip():
        return out.strip().split("/", 1)[-1]
    for candidate in ("main", "master"):
        code, _ = await run_cmd(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{candidate}"], repo, timeout=30)
        if code == 0:
            return candidate
        code, _ = await run_cmd(
            ["git", "show-ref", "--verify", "--quiet", f"refs/remotes/origin/{candidate}"], repo, timeout=30
        )
        if code == 0:
            return candidate
    branch = await current_branch(repo)
    if not branch or branch == "HEAD":
        raise GitError("Не удалось определить основную ветку (задайте TASK_BASE_BRANCH)")
    return branch


async def dirty_status(repo: Path) -> str:
    code, out = await run_cmd(["git", "status", "--porcelain", "--untracked-files=normal"], repo, timeout=60)
    if code != 0:
        raise GitError(f"git status завершился с ошибкой:\n{out.strip()[-1000:]}")
    return out.strip()


def slugify(text: str, max_len: int = 40) -> str:
    lowered = text.lower()
    transliterated = "".join(_TRANSLIT.get(ch, ch) for ch in lowered)
    slug = re.sub(r"[^a-z0-9]+", "-", transliterated).strip("-")
    if len(slug) > max_len:
        slug = slug[:max_len].rstrip("-")
    return slug or "task"


def make_branch_name(prefix: str, text: str) -> str:
    stamp = time.strftime("%Y%m%d-%H%M")
    return f"{prefix}{stamp}-{slugify(text)}"


async def prepare_task_branch(repo: Path, branch: str, base: Optional[str] = None) -> str:
    """Создаёт и переключает ветку под задачу от актуальной основной ветки.

    Возвращает имя базовой ветки. Требует чистого рабочего дерева.
    """
    dirty = await dirty_status(repo)
    if dirty:
        raise GitError(
            "Рабочее дерево не чистое — сначала закоммитьте или уберите изменения "
            "(например, попросите Codex сделать коммит, или /git stash):\n" + dirty[-1500:]
        )
    remote = await has_remote(repo)
    if remote:
        try:
            await git(["fetch", "--prune", "origin"], repo, timeout=300)
        except GitError as exc:
            log.warning("git fetch не удался: %s", exc)
    base_branch = base or await default_branch(repo)
    code, _ = await run_cmd(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{base_branch}"], repo)
    if code == 0:
        await git(["checkout", base_branch], repo)
        if remote:
            code, out = await run_cmd(["git", "pull", "--ff-only", "origin", base_branch], repo, timeout=300)
            if code != 0:
                log.warning("git pull --ff-only %s не удался: %s", base_branch, out.strip()[-300:])
    elif remote:
        await git(["checkout", "-b", base_branch, "--track", f"origin/{base_branch}"], repo)
    else:
        raise GitError(f"Ветка {base_branch} не найдена")
    await git(["checkout", "-b", branch], repo)
    return base_branch


async def recent_commits(repo: Path, base: Optional[str], branch: str, limit: int = 10) -> str:
    rev_range = f"{base}..{branch}" if base else branch
    code, out = await run_cmd(["git", "log", "--oneline", f"-{limit}", rev_range], repo, timeout=60)
    return out.strip() if code == 0 else ""


def build_task_prompt(
    task_text: str,
    *,
    repo: Path,
    branch: str,
    base: str,
    remote: Optional[str],
    auto_pr: bool,
) -> str:
    remote_line = f" (remote: {remote})" if remote else " (no remote configured)"
    pr_step = (
        f"5. Create a pull request into `{base}` with `gh pr create --base {base} --title \"...\" --body \"...\"` "
        "and print its URL. If `gh` is missing or not authenticated, skip this step and say so explicitly."
        if auto_pr
        else "5. Do not create a pull request; just make sure the branch is pushed."
    )
    push_step = (
        f"4. Push the branch: `git push -u origin {branch}`."
        if remote
        else "4. There is no remote, so skip pushing; leave the commits on the branch."
    )
    return f"""You are working in the git repository at {repo}{remote_line}.
A dedicated branch `{branch}` has already been created from `{base}` and is checked out. Do all work on this branch.

TASK (sent by the user via Telegram):
{task_text.strip()}

Requirements:
1. Explore the codebase as much as needed, then implement the task completely.
2. Run the project's existing tests, linters or build (if any) and make sure they pass; fix what you break.
3. Commit the work with clear, descriptive commit message(s). Never commit secrets, build artifacts or unrelated files.
{push_step}
{pr_step}
6. Finish with a short report in the language of the task: what changed, how it was verified, the commit hash(es) and the PR URL if one was created.

Nobody can answer clarifying questions, so make reasonable assumptions and state them in the report."""
