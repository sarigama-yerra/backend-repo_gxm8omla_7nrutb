"""
Database Schemas for the MVP

Each Pydantic model name maps to a MongoDB collection with the lowercased
class name as the collection name, e.g. Page -> "page".

We keep schemas minimal to move fast.
"""
from typing import Optional, List
from pydantic import BaseModel, Field

class Workspace(BaseModel):
    name: str = Field(..., description="Workspace display name")
    # GitHub integration fields (optional until connected)
    gh_access_token: Optional[str] = Field(None, description="GitHub token for API access")
    gh_repo_full_name: Optional[str] = Field(None, description="owner/repo")
    gh_default_branch: Optional[str] = Field("main", description="Default branch name")

class Page(BaseModel):
    title: str = Field(..., description="Page title")
    content: str = Field("", description="Markdown content")
    folder_path: str = Field("/", description="Path-like folder, e.g. /docs/specs")
    tags: List[str] = Field(default_factory=list)
    workspace_id: str = Field(..., description="Workspace this page belongs to")
    # GitHub mapping (optional until synced)
    git_path: Optional[str] = Field(None, description="Path in the repo, e.g. docs/specs/page.md")

class PageUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    folder_path: Optional[str] = None
    tags: Optional[List[str]] = None
    git_path: Optional[str] = None

class GitConnect(BaseModel):
    workspace_id: str
    access_token: str

class GitRepoSelect(BaseModel):
    workspace_id: str
    owner: str
    repo: str
    default_branch: Optional[str] = "main"

class GitSyncPage(BaseModel):
    page_id: str
    path: str  # repo path for the file
    commit_message: Optional[str] = "docs: update from workspace"

class GitPullPage(BaseModel):
    page_id: str

class SearchQuery(BaseModel):
    workspace_id: str
    q: str

class LockPayload(BaseModel):
    locked_by: Optional[str] = None
    is_locked: bool = True
