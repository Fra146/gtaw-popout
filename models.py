"""Pydantic schemas for the Second-Monitor Tactical Hub."""

from typing import Any, Literal
from pydantic import BaseModel, Field


class WidgetPreset(BaseModel):
    """Configuration for a mirrorable FiveM NUI widget preset."""

    id: str
    name: str
    selector: str
    mode: Literal["fill", "fit-width", "contain", "natural"] = "fill"
    open_command: str | None = None
    launchable: bool = False
    hide_in_game: bool = True
    default_size: tuple[int, int] = (600, 400)


class NuiStateMessage(BaseModel):
    """WebSocket payload streaming NUI DOM/canvas state to popout windows."""

    selector: str
    state: Literal["ACTIVE", "STANDBY", "DISCONNECTED"]
    html: str | None = None
    styles: list[str] = Field(default_factory=list)
    canvases: dict[str, str] = Field(default_factory=dict)
    timestamp: float | None = None
    error: str | None = None


class CommsSpeaker(BaseModel):
    """Normalized representation of a voice channel participant."""

    id: str
    name: str
    speaking: bool = False
    muted: bool = False
    deafened: bool = False


class CommsStateMessage(BaseModel):
    """WebSocket payload streaming voice-activity updates."""

    source: Literal["teamspeak", "discord"]
    connected: bool
    channel: str | None = None
    speakers: list[CommsSpeaker] = Field(default_factory=list)
    error: str | None = None


class LaunchRequest(BaseModel):
    """REST payload to explicitly dispatch an in-game NUI open command."""

    selector: str
    command: str | None = None


class VisibilityRequest(BaseModel):
    """REST payload to toggle in-game hiding for a mirrored selector."""

    selector: str
    hide_in_game: bool


class CommsConnectRequest(BaseModel):
    """REST payload to connect or configure a voice comms bridge."""

    source: Literal["teamspeak", "discord"]
    api_key: str | None = None
    host: str = "127.0.0.1"
    port: int = 25639


class DeckWidgetConfig(BaseModel):
    """Single widget slot inside a composable /deck layout."""

    type: Literal["els", "radio", "comms", "popout"]
    title: str | None = None
    span: Literal[1, 2] = 1
    height: int = 320
    visible: bool = True
    params: dict[str, Any] = Field(default_factory=dict)


class DeckLayout(BaseModel):
    """Saved or built-in multi-device layout preset for /deck."""

    id: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    name: str = Field(..., min_length=1, max_length=64)
    columns: Literal["auto", "1", "2", "3"] = "auto"
    density: Literal["compact", "normal", "touch"] = "normal"
    widgets: list[DeckWidgetConfig] = Field(default_factory=list)
    builtin: bool = False
