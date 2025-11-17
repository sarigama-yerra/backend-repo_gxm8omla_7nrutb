import os
from typing import List, Optional
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from bson import ObjectId
import base64
import requests

from database import db, create_document, get_documents
from schemas import Workspace, Page, PageUpdate, GitConnect, GitRepoSelect, GitSyncPage, GitPullPage, SearchQuery, LockPayload

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Utilities

def to_object_id(id_str: str) -> ObjectId:
    try:
        return ObjectId(id_str)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid id")


def serialize(doc):
    if not doc:
        return doc
    doc["id"] = str(doc.pop("_id"))
    # Hide sensitive token if present
    if "gh_access_token" in doc:
        doc["gh_connected"] = bool(doc.get("gh_access_token"))
        doc.pop("gh_access_token", None)
    return doc


@app.get("/")
def read_root():
    return {"message": "Docs+Git MVP API"}


@app.get("/test")
def test_database():
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
    }
    try:
        _ = db.list_collection_names()
        response["database"] = "✅ Connected"
    except Exception as e:
        response["database"] = f"❌ {str(e)[:120]}"
    return response


# Workspaces
@app.post("/workspaces")
def create_workspace(payload: Workspace):
    ws_id = create_document("workspace", payload)
    doc = db["workspace"].find_one({"_id": ObjectId(ws_id)})
    return serialize(doc)


@app.get("/workspaces")
def list_workspaces():
    items = list(db["workspace"].find().limit(50))
    return [serialize(x) for x in items]


# Pages CRUD
@app.post("/pages")
def create_page(payload: Page):
    # Ensure workspace exists
    ws = db["workspace"].find_one({"_id": to_object_id(payload.workspace_id)})
    if not ws:
        raise HTTPException(status_code=404, detail="Workspace not found")
    page_id = create_document("page", payload)
    doc = db["page"].find_one({"_id": ObjectId(page_id)})
    return serialize(doc)


@app.get("/pages")
def list_pages(workspace_id: str, folder_path: Optional[str] = None):
    query = {"workspace_id": workspace_id}
    if folder_path:
        query["folder_path"] = folder_path
    items = list(db["page"].find(query).limit(200))
    return [serialize(x) for x in items]


@app.get("/pages/{page_id}")
def get_page(page_id: str):
    doc = db["page"].find_one({"_id": to_object_id(page_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Page not found")
    return serialize(doc)


@app.patch("/pages/{page_id}")
def update_page(page_id: str, payload: PageUpdate):
    update = {k: v for k, v in payload.model_dump(exclude_none=True).items()}
    if not update:
        return get_page(page_id)
    db["page"].update_one({"_id": to_object_id(page_id)}, {"$set": update})
    return get_page(page_id)


@app.delete("/pages/{page_id}")
def delete_page(page_id: str):
    res = db["page"].delete_one({"_id": to_object_id(page_id)})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Page not found")
    return {"ok": True}


# Simple locks (no realtime) for basic collaboration
@app.post("/pages/{page_id}/lock")
def lock_page(page_id: str, payload: LockPayload):
    db["page"].update_one({"_id": to_object_id(page_id)}, {"$set": {"lock": payload.model_dump()}})
    return get_page(page_id)

@app.post("/pages/{page_id}/unlock")
def unlock_page(page_id: str):
    db["page"].update_one({"_id": to_object_id(page_id)}, {"$unset": {"lock": ""}})
    return get_page(page_id)


# GitHub OAuth token save (we accept a pre-obtained token for MVP)
@app.post("/github/connect")
def github_connect(payload: GitConnect):
    ws_id = to_object_id(payload.workspace_id)
    db["workspace"].update_one({"_id": ws_id}, {"$set": {"gh_access_token": payload.access_token}})
    return serialize(db["workspace"].find_one({"_id": ws_id}))


# List repos for the connected user (MVP)
@app.get("/github/repos")
def github_list_repos(workspace_id: str):
    ws = db["workspace"].find_one({"_id": to_object_id(workspace_id)})
    if not ws or not ws.get("gh_access_token"):
        raise HTTPException(status_code=400, detail="GitHub not connected")
    headers = {"Authorization": f"token {ws['gh_access_token']}", "Accept": "application/vnd.github+json"}
    r = requests.get("https://api.github.com/user/repos?per_page=100", headers=headers, timeout=20)
    if r.status_code >= 300:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    data = r.json()
    return [{"full_name": x["full_name"], "default_branch": x.get("default_branch", "main")} for x in data]


@app.post("/github/select-repo")
def github_select_repo(payload: GitRepoSelect):
    ws_id = to_object_id(payload.workspace_id)
    db["workspace"].update_one({"_id": ws_id}, {"$set": {"gh_repo_full_name": f"{payload.owner}/{payload.repo}", "gh_default_branch": payload.default_branch}})
    return serialize(db["workspace"].find_one({"_id": ws_id}))


# Sync a page -> repo path (create or update via GitHub Contents API)
@app.post("/github/sync-page")
def github_sync_page(payload: GitSyncPage):
    page = db["page"].find_one({"_id": to_object_id(payload.page_id)})
    if not page:
        raise HTTPException(status_code=404, detail="Page not found")
    ws = db["workspace"].find_one({"_id": to_object_id(page["workspace_id"])})
    if not ws or not ws.get("gh_access_token") or not ws.get("gh_repo_full_name"):
        raise HTTPException(status_code=400, detail="GitHub not configured for workspace")

    owner_repo = ws["gh_repo_full_name"]
    branch = ws.get("gh_default_branch", "main")
    path = payload.path

    headers = {
        "Authorization": f"token {ws['gh_access_token']}",
        "Accept": "application/vnd.github+json"
    }
    # Get current file sha if exists
    get_url = f"https://api.github.com/repos/{owner_repo}/contents/{path}?ref={branch}"
    sha = None
    r = requests.get(get_url, headers=headers, timeout=20)
    if r.status_code == 200:
        sha = r.json().get("sha")

    content_b64 = base64.b64encode(page.get("content", "").encode("utf-8")).decode("utf-8")
    put_url = f"https://api.github.com/repos/{owner_repo}/contents/{path}"
    body = {
        "message": payload.commit_message or "docs: update from workspace",
        "content": content_b64,
        "branch": branch
    }
    if sha:
        body["sha"] = sha
    r = requests.put(put_url, headers=headers, json=body, timeout=20)
    if r.status_code >= 300:
        raise HTTPException(status_code=r.status_code, detail=r.text)

    # Save git_path mapping on page
    db["page"].update_one({"_id": page["_id"]}, {"$set": {"git_path": path}})
    return get_page(str(page["_id"]))


# Pull changes from repo -> page content (one file)
@app.post("/github/pull-page")
def github_pull_page(payload: GitPullPage):
    page = db["page"].find_one({"_id": to_object_id(payload.page_id)})
    if not page or not page.get("git_path"):
        raise HTTPException(status_code=400, detail="Page not synced to a git path yet")

    ws = db["workspace"].find_one({"_id": to_object_id(page["workspace_id"])})
    if not ws or not ws.get("gh_access_token") or not ws.get("gh_repo_full_name"):
        raise HTTPException(status_code=400, detail="GitHub not configured for workspace")

    owner_repo = ws["gh_repo_full_name"]
    branch = ws.get("gh_default_branch", "main")
    headers = {
        "Authorization": f"token {ws['gh_access_token']}",
        "Accept": "application/vnd.github+json"
    }
    get_url = f"https://api.github.com/repos/{owner_repo}/contents/{page['git_path']}?ref={branch}"
    r = requests.get(get_url, headers=headers, timeout=20)
    if r.status_code >= 300:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    data = r.json()
    if data.get("encoding") == "base64" and data.get("content"):
        content = base64.b64decode(data["content"]).decode("utf-8")
        db["page"].update_one({"_id": page["_id"]}, {"$set": {"content": content}})
    return get_page(str(page["_id"]))


# History listing via Git log (GitHub API commits for a path)
@app.get("/github/history")
def github_history(workspace_id: str, path: str):
    ws = db["workspace"].find_one({"_id": to_object_id(workspace_id)})
    if not ws or not ws.get("gh_access_token") or not ws.get("gh_repo_full_name"):
        raise HTTPException(status_code=400, detail="GitHub not configured for workspace")
    headers = {"Authorization": f"token {ws['gh_access_token']}", "Accept": "application/vnd.github+json"}
    owner_repo = ws["gh_repo_full_name"]
    r = requests.get(f"https://api.github.com/repos/{owner_repo}/commits", params={"path": path, "per_page": 50}, headers=headers, timeout=20)
    if r.status_code >= 300:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    data = r.json()
    # Return compact info
    return [
        {
            "sha": x.get("sha"),
            "author": (x.get("commit", {}).get("author", {}) or {}).get("name"),
            "date": (x.get("commit", {}).get("author", {}) or {}).get("date"),
            "message": (x.get("commit", {}) or {}).get("message"),
            "url": x.get("html_url"),
        }
        for x in data
    ]


# Search (simple full text using regex on title/content/tags)
@app.get("/search")
def search(workspace_id: str, q: str):
    try:
        regex = {"$regex": q, "$options": "i"}
        items = list(db["page"].find({"workspace_id": workspace_id, "$or": [{"title": regex}, {"content": regex}, {"tags": regex}]}).limit(50))
        return [serialize(x) for x in items]
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
