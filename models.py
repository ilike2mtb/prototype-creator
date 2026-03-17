from typing import Optional, Union

from pydantic import BaseModel, ConfigDict, Field


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    framework: str = "drupal11"
    output_type: str = "both"
    mode: str = "arch"
    drupal_version: Optional[str] = None
    figma_params: Optional[dict] = None


class ChatResponse(BaseModel):
    message: str
    artifacts: list[dict] = Field(default_factory=list)


class UpstreamErrorDetail(BaseModel):
    message: str
    figma_status: Optional[int] = None
    figma_body: Optional[str] = None


class ErrorResponse(BaseModel):
    detail: Union[str, UpstreamErrorDetail]


class FigmaNode(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: Optional[str] = None
    name: Optional[str] = None
    type: Optional[str] = None
    layoutMode: Optional[str] = None
    itemSpacing: Optional[float] = None
    paddingTop: Optional[float] = None
    paddingBottom: Optional[float] = None
    paddingLeft: Optional[float] = None
    paddingRight: Optional[float] = None
    children: list["FigmaNode"] = Field(default_factory=list)


class FigmaFileResponse(BaseModel):
    name: Optional[str] = None
    lastModified: Optional[str] = None
    version: Optional[str] = None
    document: Optional[FigmaNode] = None


class FigmaNodesResponse(BaseModel):
    name: Optional[str] = None
    nodes: dict[str, Optional[FigmaNode]] = Field(default_factory=dict)


class FigmaImagesResponse(BaseModel):
    images: dict[str, Optional[str]] = Field(default_factory=dict)
    err: Optional[str] = None


class FigmaFrame(BaseModel):
    id: Optional[str] = None
    name: Optional[str] = None
    type: Optional[str] = None
    path: Optional[str] = None
    parentId: Optional[str] = None
    childCount: int = 0
    layoutMode: Optional[str] = None
    itemSpacing: Optional[float] = None
    paddingTop: Optional[float] = None
    paddingBottom: Optional[float] = None
    paddingLeft: Optional[float] = None
    paddingRight: Optional[float] = None
    x: Optional[float] = None
    y: Optional[float] = None
    width: Optional[float] = None
    height: Optional[float] = None


class FigmaFramesResponse(BaseModel):
    name: Optional[str] = None
    requestedIds: Optional[str] = None
    depth: Optional[int] = None
    totalFrames: int
    frames: list[FigmaFrame] = Field(default_factory=list)


class FigmaExportedImagesResponse(BaseModel):
    totalFrames: int
    frameIds: list[str] = Field(default_factory=list)
    images: dict[str, Optional[str]] = Field(default_factory=dict)
    batchSize: Optional[int] = None
    batchCount: Optional[int] = None
    err: Optional[str] = None


class FigmaVariable(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None
    collection: str = ""
    hex: Optional[str] = None
    value: Optional[str] = None


class FigmaVariablesResponse(BaseModel):
    variableCount: int
    collections: list[str] = Field(default_factory=list)
    variables: list[FigmaVariable] = Field(default_factory=list)


class FigmaComponent(BaseModel):
    name: Optional[str] = None
    description: str = ""
    nodeId: Optional[str] = None
    componentSetId: Optional[str] = None


class FigmaComponentsResponse(BaseModel):
    componentCount: int
    components: list[FigmaComponent] = Field(default_factory=list)


class FigmaStyle(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None
    description: str = ""
    nodeId: Optional[str] = None


class FigmaStylesResponse(BaseModel):
    styleCount: int
    styles: list[FigmaStyle] = Field(default_factory=list)


FigmaNode.model_rebuild()
