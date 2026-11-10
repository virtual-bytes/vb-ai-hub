"""
title: VB AI Hub
author: Virtual Bytes
version: 1.0.0
description: Search and ask the VB AI Hub project library, get a project brief (latest handoff, open tasks), and file the current chat under a project and task.
"""

import json
import urllib.error
import urllib.parse
import urllib.request

from pydantic import BaseModel, Field


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Every call carries the hub token, so a redirect to another address is an error rather than a hand-over."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, f"the hub redirected to {newurl}; set hub_url to that address", headers, fp)


OPENER = urllib.request.build_opener(NoRedirect)


class Tools:
    class Valves(BaseModel):
        hub_url: str = Field(default="http://localhost:8787", description="VB AI Hub address")
        hub_token: str = Field(default="", description="VB AI Hub access token (HUB_TOKEN)")
        timeout: int = Field(default=180, description="Seconds to wait for answers (asking runs a model on the hub)")

    def __init__(self):
        self.valves = self.Valves()

    def _call(self, method: str, path: str, body=None):
        req = urllib.request.Request(
            self.valves.hub_url.rstrip("/") + path,
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={"Authorization": f"Bearer {self.valves.hub_token}", "Content-Type": "application/json"},
        )
        try:
            with OPENER.open(req, timeout=self.valves.timeout) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read()).get("error", e.reason)
            except Exception:
                msg = e.reason
            raise RuntimeError(f"VB AI Hub returned {e.code}: {msg}")

    def _project_id(self, name: str):
        if not name:
            return None
        for p in self._call("GET", "/api/projects").get("projects", []):
            if p["name"].lower() == name.strip().lower():
                return p["id"]
        return None

    def search_library(self, query: str) -> str:
        """
        Keyword search across the VB AI Hub library: documents, handoffs, work logs, notes, projects and tasks.
        :param query: Words to search for.
        :return: Matching entries with project, title and a snippet.
        """
        res = self._call("GET", "/api/search?q=" + urllib.parse.quote(query)).get("results", [])
        if not res:
            return "No matches in the VB AI Hub library."
        lines = []
        for r in res[:15]:
            snip = r["snip"].replace("[[", "**").replace("]]", "**")
            title = f"{r['title']}: {r['card_title']}" if r["kind"] == "doc" else r["card_title"]
            lines.append(f"- [{r['kind']}] {title} ({r.get('project') or 'no project'}): {snip}")
        return "Results from VB AI Hub:\n" + "\n".join(lines)

    def ask_library(self, question: str, project: str = "") -> str:
        """
        Ask the VB AI Hub library a question. It answers from the team's docs, handoffs and work logs, with sources.
        :param question: The question to answer.
        :param project: Optional project name to limit the search to.
        :return: An answer with numbered sources.
        """
        res = self._call("POST", "/api/ask", {"question": question, "project_id": self._project_id(project)})
        src = "\n".join(f"[{i}] {s['title']} ({s['kind']}, {s.get('project') or 'no project'})" for i, s in enumerate(res.get("sources", []), 1))
        return f"{res.get('answer', '')}\n\nSources:\n{src}" if src else res.get("answer", "")

    def project_brief(self, project: str) -> str:
        """
        Get a brief for a VB AI Hub project: description, goals, links, open tasks, recent items and the latest handoff.
        :param project: The project name.
        :return: The brief as Markdown.
        """
        b = self._call("GET", "/api/tools/brief?project=" + urllib.parse.quote(project))
        out = [f"# {b['project']} ({b['status']})"]
        if b.get("description"):
            out += ["", b["description"]]
        if b.get("goals"):
            out += ["", "## Goals", b["goals"]]
        if b.get("links"):
            out += ["", "## Links"] + [f"- {l.get('label') or l['url']}: {l['url']}" for l in b["links"]]
        if b.get("open_tasks"):
            out += ["", "## Open tasks"] + [f"- {t['title']} ({t['priority']}{', due ' + t['due'] if t['due'] else ''})" for t in b["open_tasks"]]
        if b.get("recent_items"):
            out += ["", "## Recent items"] + [f"- {c['title']} ({c['source']}, {c['last_activity']})" for c in b["recent_items"]]
        if b.get("latest_handoff"):
            out += ["", f"## Latest handoff: {b['latest_handoff']['name']}", "", b["latest_handoff"]["text"]]
        return "\n".join(out)

    def file_this_chat(self, project: str, task: str = "", __metadata__: dict = None) -> str:
        """
        File the current chat in VB AI Hub under a project, and optionally a task (created if it doesn't exist).
        :param project: Project name (created if it doesn't exist).
        :param task: Optional task title within that project.
        :return: Confirmation with a link to the item in the hub.
        """
        chat_id = (__metadata__ or {}).get("chat_id")
        if not chat_id or str(chat_id).startswith("local"):
            return "This chat has no saved id yet (temporary chats can't be filed). Send a message first, then try again."
        r = self._call("POST", "/api/tools/file-chat", {"chat_id": chat_id, "project": project, "task": task})
        where = f"project **{r['project']}**" + (f", task **{task}**" if task else "")
        return f"Filed this chat in VB AI Hub under {where}." + (f" {r['url']}" if r.get("url") else "")
