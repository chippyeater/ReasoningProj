import { useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import {
  createCase,
  createInfoUnit,
  appendFilesToCase,
  deleteCase,
  deleteInfoUnit,
  getCase,
  getCaseById,
  listCases,
  listQuoteUnits,
  selectCase,
  generateInference,
  routeTask,
  runExtraction,
  runReasoning,
  askQa,
  actInteraction,
  renameCase,
  updateCardPosition,
  updateCardDisplayLevel,
  updateInfoUnit,
  updateWorkspaceState,
  type CaseData,
  type CaseSummary,
  type InferenceCard,
  type InfoUnit,
  type InfoUnitSubtype,
  type InfoUnitType,
  type InfoUnitMutationInput,
  type QuoteUnit,
} from "./api";
import myAvatar from "./assets/my-avatar.png";
import folderIcon from "./assets/folder.png";
import chevronsUpIcon from "./assets/chevrons-up.png";
import searchIcon from "./assets/search.png";
import filterIcon from "./assets/filter.png";
import uploadIcon from "./assets/upload.png";
import detailsIcon from "./assets/details.png";
import writeIcon from "./assets/write.png";
import sendIcon from "./assets/send.png";
import metaTypeDefaultIcon from "./assets/meta-type-default.png";
import inferTypeDefaultIcon from "./assets/infer-type-default.png";
import docIcon from "./assets/file-types/Property 1=DOC.png";
import jpgIcon from "./assets/file-types/Property 1=JPG.png";
import mp3Icon from "./assets/file-types/Property 1=MP3.png";
import pdfIcon from "./assets/file-types/Property 1=PDF.png";
import pngIcon from "./assets/file-types/Property 1=PNG.png";
import pptIcon from "./assets/file-types/Property 1=PPT.png";
import svgIcon from "./assets/file-types/Property 1=SVG.png";
import txtIcon from "./assets/file-types/Property 1=TXT.png";
import xlsIcon from "./assets/file-types/Property 1=XLS.png";
import zipIcon from "./assets/file-types/Property 1=ZIP.png";

type EvidenceStatus = "pending" | "submitted" | "success";

type AddedEvidence = {
  id: string;
  status: EvidenceStatus;
  type: "document" | "image" | "video" | "audio";
  file: File;
};

type CanvasKind = "person" | "org" | "object" | "event" | "claim" | "law" | "evidence";

type CanvasDetailField = {
  key: string;
  value: string;
};

type CanvasNode = {
  id: string;
  label: string;
  meta: string;
  actionLabel?: string;
  detailFields: CanvasDetailField[];
  kind: CanvasKind;
  source: "meta" | "inference" | "law" | "evidence" | "placeholder";
  level: 1 | 2 | 3;
  state?: "normal" | "missing";
  semanticRole?: "default" | "conflict";
  conflictDetail?: ConflictDetail;
  x: number;
  y: number;
};

type ConflictNodeState = "candidate" | "pending" | "conclusion" | "muted";

type ConflictReferenceItem = {
  id: string;
  title: string;
  meta: string;
};

type ConflictDetail = {
  state: ConflictNodeState;
  stateLabel: string;
  decisionLabel: string;
  claim: string;
  confidenceLabel: string | null;
  supportItems: ConflictReferenceItem[];
  opposeItems: ConflictReferenceItem[];
  missingItems: ConflictReferenceItem[];
  reasoningSteps: string[];
};

type CanvasEdge = {
  id: string;
  sourceId: string;
  targetId: string;
  relationType: string;
  strength: number;
};

type Point = { x: number; y: number };
type NodeSize = { width: number; height: number };
type CanvasTransform = Point & { zoom: number };

type FileTreeItem = {
  id: string;
  name: string;
  type: "document" | "image";
  status: string;
  source: "queued" | "parsed";
  subText: string;
};

type InfoUnitEditorState = {
  mode: "create" | "edit";
  infoUnitId: string | null;
};

type InfoUnitDraft = {
  type: InfoUnitType;
  subtype: InfoUnitSubtype | null;
  title: string;
  content: string;
  speaker: string;
  side: string;
  actor: string;
  target: string;
  relatedIssue: string;
};

const INFO_UNIT_TYPES: InfoUnitType[] = ["entity", "event", "state", "rule", "position"];
const ENTITY_SUBTYPES: InfoUnitSubtype[] = ["person", "organization", "object", "location"];
const EVENT_SUBTYPES: InfoUnitSubtype[] = ["reported", "documented", "verified"];

function emptyInfoUnitDraft(): InfoUnitDraft {
  return {
    type: "event",
    subtype: null,
    title: "",
    content: "",
    speaker: "",
    side: "",
    actor: "",
    target: "",
    relatedIssue: "",
  };
}

function infoUnitDraft(unit: InfoUnit): InfoUnitDraft {
  return {
    type: unit.type,
    subtype: unit.subtype,
    title: unit.title,
    content: unit.content,
    speaker: unit.speaker ?? "",
    side: unit.side ?? "",
    actor: unit.actor ?? "",
    target: unit.target ?? "",
    relatedIssue: unit.related_issue ?? "",
  };
}

function nullableText(value: string): string | null {
  const normalized = value.trim();
  return normalized || null;
}

function quoteMatchesSource(unit: QuoteUnit, ref: InfoUnit["source_refs"][number]): boolean {
  return unit.file_id === ref.file_id && unit.start === ref.start && unit.end === ref.end && unit.text === ref.quote;
}

const CANVAS_WIDTH = 3200;
const CANVAS_HEIGHT = 2200;
const NODE_WIDTH = 230;
const NODE_HEIGHT = 120;
const DEFAULT_NODE_SIZE: NodeSize = { width: NODE_WIDTH, height: NODE_HEIGHT };
const ISLAND_WIDTH = 350;
const ISLAND_MIN_HEIGHT = 38;
const ISLAND_MAX_INPUT_HEIGHT = 120;
const ISLAND_PADDING = 8;
const ISLAND_BOTTOM_DOCK_THRESHOLD = 28;
const MIN_CANVAS_ZOOM = 0.25;
const MAX_CANVAS_ZOOM = 2.5;
const FIT_NODES_PADDING = 64;
const VIEWPORT_SAVE_DELAY_MS = 180;
const DISPLAY_LEVEL_SAVE_DELAY_MS = 160;

function clampZoom(zoom: number): number {
  return Math.min(MAX_CANVAS_ZOOM, Math.max(MIN_CANVAS_ZOOM, zoom));
}

function clampViewport(transform: CanvasTransform, viewportWidth: number, viewportHeight: number): CanvasTransform {
  const canvasWidth = CANVAS_WIDTH * transform.zoom;
  const canvasHeight = CANVAS_HEIGHT * transform.zoom;
  const centeredX = Math.max(0, (viewportWidth - canvasWidth) / 2);
  const centeredY = Math.max(0, (viewportHeight - canvasHeight) / 2);
  const minX = viewportWidth >= canvasWidth ? centeredX : viewportWidth - canvasWidth;
  const maxX = viewportWidth >= canvasWidth ? centeredX : 0;
  const minY = viewportHeight >= canvasHeight ? centeredY : viewportHeight - canvasHeight;
  const maxY = viewportHeight >= canvasHeight ? centeredY : 0;
  return {
    x: Math.min(maxX, Math.max(minX, transform.x)),
    y: Math.min(maxY, Math.max(minY, transform.y)),
    zoom: transform.zoom,
  };
}

function clampViewportToWrapper(transform: CanvasTransform, wrapper: HTMLDivElement | null): CanvasTransform {
  if (!wrapper) return transform;
  const rect = wrapper.getBoundingClientRect();
  return clampViewport(transform, rect.width, rect.height);
}

function stepDisplayLevel(currentLevel: 1 | 2 | 3, direction: "up" | "down"): 1 | 2 | 3 {
  if (direction === "up") return currentLevel === 1 ? 2 : currentLevel === 2 ? 3 : 3;
  return currentLevel === 3 ? 2 : currentLevel === 2 ? 1 : 1;
}

function inferType(file: File): "document" | "image" | "video" | "audio" {
  if (file.type.startsWith("image/")) return "image";
  if (file.type.startsWith("video/")) return "video";
  if (file.type.startsWith("audio/")) return "audio";
  return "document";
}

function firstLine(text?: string) {
  return (text ?? "").split(/\r?\n/, 1)[0] ?? "";
}

function evidenceStatusLabel(status: EvidenceStatus) {
  if (status === "submitted") return "submitted";
  if (status === "success") return "success";
  return "pending";
}

function parseStatusLabel(status?: string) {
  if (status === "parsed") return "success";
  if (status === "parsing") return "partial";
  if (status === "failed") return "unsupported";
  return "unknown";
}

function kindFromInfoType(type: InfoUnitType): CanvasKind {
  if (type === "event") return "event";
  if (type === "position") return "claim";
  if (type === "rule") return "law";
  if (type === "entity") return "person";
  return "object";
}


function infoTypeLabel(type: InfoUnitType): string {
  if (type === "entity") return "实体";
  if (type === "event") return "事件";
  if (type === "state") return "状态";
  if (type === "rule") return "规则";
  if (type === "position") return "立场";
  return type;
}

function conflictDecisionLabel(decision: InferenceCard["decision"]): string {
  if (decision === "accepted") return "已形成结论";
  if (decision === "rejected") return "已排除";
  if (decision === "pending") return "待确认";
  return "候选冲突";
}

function conflictNodeState(card: InferenceCard): ConflictNodeState {
  if (card.status !== "active") return "muted";
  if (card.decision === "accepted" || card.decision === "rejected") return "conclusion";
  if (card.decision === "pending") return "pending";
  return "candidate";
}

function conflictStateLabel(state: ConflictNodeState): string {
  if (state === "conclusion") return "结论态";
  if (state === "pending") return "待确认";
  if (state === "muted") return "灰态";
  return "候选态";
}

function formatConflictConfidence(confidence?: number | null): string | null {
  if (confidence === null || confidence === undefined || Number.isNaN(confidence)) return null;
  const normalized = confidence <= 1 ? Math.round(confidence * 100) : Math.round(confidence);
  return `置信度 ${normalized}%`;
}

function resolveConflictReference(caseData: CaseData, cardId: string): ConflictReferenceItem {
  const normalizedId = cardId.trim();
  const metaCard = caseData.meta_cards.find((card) => card.id === normalizedId);
  if (metaCard) {
    return {
      id: normalizedId,
      title: metaCard.title,
      meta: firstLine(metaCard.summary || metaCard.detail.description || infoTypeLabel(metaCard.info_type)) || "元信息",
    };
  }

  const inferenceCard = caseData.inference_cards.find((card) => card.id === normalizedId);
  if (inferenceCard) {
    return {
      id: normalizedId,
      title: inferenceCard.title,
      meta: firstLine(inferenceCard.detail.claim || conflictDecisionLabel(inferenceCard.decision)) || "推理",
    };
  }

  const lawCard = caseData.law_cards.find((card) => card.id === normalizedId);
  if (lawCard) {
    return {
      id: normalizedId,
      title: lawCard.title,
      meta: firstLine(lawCard.detail.text || lawCard.source_type) || "法条",
    };
  }

  return {
    id: normalizedId,
    title: "待补充证据",
    meta: normalizedId,
  };
}

function buildConflictDetail(caseData: CaseData, card: InferenceCard): ConflictDetail {
  const state = conflictNodeState(card);
  return {
    state,
    stateLabel: conflictStateLabel(state),
    decisionLabel: conflictDecisionLabel(card.decision),
    claim: firstLine(card.detail.claim) || firstLine(card.summary || undefined) || card.title,
    confidenceLabel: formatConflictConfidence(card.detail.confidence),
    supportItems: card.detail.supporting_card_ids.map((id) => resolveConflictReference(caseData, id)),
    opposeItems: card.detail.opposing_card_ids.map((id) => resolveConflictReference(caseData, id)),
    missingItems: card.detail.missing_card_ids.map((id) => resolveConflictReference(caseData, id)),
    reasoningSteps: card.detail.reasoning_steps.map((step) => step.trim()).filter(Boolean),
  };
}

function findMatchedInfoUnit(caseData: CaseData, card: CaseData["meta_cards"][number]) {
  const exact = caseData.info_units.find(
    (unit) => unit.type === card.info_type && unit.subtype === card.info_subtype && unit.title === card.title,
  );
  if (exact) return exact;

  const byTypeAndTitle = caseData.info_units.find(
    (unit) => unit.type === card.info_type && unit.title === card.title,
  );
  if (byTypeAndTitle) return byTypeAndTitle;

  return caseData.info_units.find((unit) => unit.title === card.title);
}

function formatDetailValue(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  if (typeof value === "string") {
    const trimmed = value.trim();
    return trimmed.length > 0 ? trimmed : null;
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  if (Array.isArray(value)) {
    const parts = value
      .map((item) => formatDetailValue(item))
      .filter((item): item is string => Boolean(item));
    if (parts.length === 0) return null;
    return parts.join(", ");
  }
  if (typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>)
      .map(([k, v]) => {
        const normalized = formatDetailValue(v);
        if (!normalized) return null;
        return `${k}=${normalized}`;
      })
      .filter((item): item is string => Boolean(item));
    if (entries.length === 0) return null;
    return entries.join(", ");
  }
  return null;
}

function buildDetailFields(fields: Array<[string, unknown]>): CanvasDetailField[] {
  return fields
    .map(([key, raw]) => {
      const value = formatDetailValue(raw);
      if (!value) return null;
      return { key, value };
    })
    .filter((item): item is CanvasDetailField => Boolean(item));
}

function edgeStrength(edgeType: string) {
  if (edgeType === "supports" || edgeType === "grounded_in" || edgeType === "retrieved_for") return 3;
  if (edgeType === "opposes" || edgeType === "conflicts_with" || edgeType === "missing_for") return 2;
  return 1;
}

function edgeTypeLabel(edgeType: string, fallback?: string | null) {
  if (fallback && fallback.trim()) return fallback;
  const labels: Record<string, string> = {
    relates_to: "\u76f8\u5173",
    supports: "\u652f\u6301",
    opposes: "\u53cd\u9a73",
    derives: "\u63a8\u5bfc",
    mentions: "\u63d0\u53ca",
    conflicts_with: "\u51b2\u7a81",
    missing_for: "\u7f3a\u5931\u4e8e",
    cites: "\u5f15\u7528",
    belongs_to: "\u5c5e\u4e8e",
    grounded_in: "\u4f9d\u636e\u4e8e",
    applies_to: "\u9002\u7528\u4e8e",
    retrieved_for: "\u4e3a\u6b64\u68c0\u7d22",
  };
  return labels[edgeType] ?? edgeType;
}

function readableBytes(size: number) {
  if (size <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let value = size;
  let idx = 0;
  while (value >= 1024 && idx < units.length - 1) {
    value /= 1024;
    idx += 1;
  }
  return `${value.toFixed(value >= 100 ? 0 : 1)} ${units[idx]}`;
}

function formatFileDate(ts: number | string) {
  const value = typeof ts === "string" ? Date.parse(ts) : ts;
  return new Date(value).toLocaleDateString("en-GB", {
    day: "2-digit",
    month: "short",
    year: "numeric",
  });
}

function formatUploadFileNames(files: File[]) {
  const names = files.map((file) => file.name.trim()).filter(Boolean);
  if (names.length === 0) return "";
  if (names.length <= 3) return names.join(", ");
  return `${names.slice(0, 3).join(", ")}...`;
}

function pickFileIcon(type: "document" | "image", fileName: string) {
  const ext = fileName.split(".").pop()?.toLowerCase() ?? "";
  if (ext === "pdf") return pdfIcon;
  if (ext === "ppt" || ext === "pptx") return pptIcon;
  if (ext === "xls" || ext === "xlsx") return xlsIcon;
  if (ext === "doc" || ext === "docx") return docIcon;
  if (ext === "txt" || ext === "md") return txtIcon;
  if (ext === "jpg" || ext === "jpeg") return jpgIcon;
  if (ext === "png") return pngIcon;
  if (ext === "svg") return svgIcon;
  if (ext === "mp3" || ext === "wav") return mp3Icon;
  if (ext === "zip" || ext === "rar" || ext === "7z") return zipIcon;
  if (type === "image") return pngIcon;
  return docIcon;
}

function inferGenerateModeFromPrompt(
  prompt: string,
): "hypothesis" | "conflict_check" | "missing_evidence" {
  const normalized = prompt.trim().toLowerCase();
  if (!normalized) return "hypothesis";

  const hasKeyword = (keywords: string[]) => keywords.some((keyword) => normalized.includes(keyword));

  if (hasKeyword(["\u51b2\u7a81", "\u77db\u76fe", "\u5bf9\u6bd4", "\u6bd4\u8f83", "\u51b2\u7a81\u89c6\u56fe", "conflict", "contradict", "oppose"])) {
    return "conflict_check";
  }

  if (hasKeyword(["\u7f3a\u5931", "\u5f85\u9a8c\u8bc1", "\u8865\u8bc1", "\u8bc1\u636e\u4e0d\u8db3", "\u5f85\u8865", "missing", "lack", "insufficient"])) {
    return "missing_evidence";
  }

  return "hypothesis";
}

export default function App() {
  const [evidences, setEvidences] = useState<AddedEvidence[]>([]);
  const [currentCase, setCurrentCase] = useState<CaseData | null>(null);
  const [caseLoading, setCaseLoading] = useState(false);
  const [error, setError] = useState("");
  const [isDropActive, setIsDropActive] = useState(false);
  const [isDropSelected, setIsDropSelected] = useState(false);
  const [searchKeyword, setSearchKeyword] = useState("");
  const [selectedFileId, setSelectedFileId] = useState<string | null>(null);
  const [selectedNodeIds, setSelectedNodeIds] = useState<string[]>([]);
  const [focusedNodeId, setFocusedNodeId] = useState<string | null>(null);
  const [draggingNodeId, setDraggingNodeId] = useState<string | null>(null);
  const [uploadProgress, setUploadProgress] = useState(0);
  const [uploadStage, setUploadStage] = useState<"idle" | "uploading">("idle");
  const [uploadDisplayText, setUploadDisplayText] = useState("");
  const [caseOptions, setCaseOptions] = useState<CaseSummary[]>([]);
  const [selectedCaseId, setSelectedCaseId] = useState("");
  const [caseMenuOpen, setCaseMenuOpen] = useState(false);
  const [infoUnitEditor, setInfoUnitEditor] = useState<InfoUnitEditorState | null>(null);
  const [infoUnitDraftState, setInfoUnitDraftState] = useState<InfoUnitDraft>(emptyInfoUnitDraft);
  const [quoteUnits, setQuoteUnits] = useState<QuoteUnit[]>([]);
  const [selectedEvidenceIds, setSelectedEvidenceIds] = useState<string[]>([]);
  const [evidenceChanged, setEvidenceChanged] = useState(false);
  const [quoteSearch, setQuoteSearch] = useState("");
  const [editorError, setEditorError] = useState("");
  const [editorBusy, setEditorBusy] = useState(false);
  const [quotesLoading, setQuotesLoading] = useState(false);
  const [extractionRunning, setExtractionRunning] = useState(false);

  const [transform, setTransform] = useState<CanvasTransform>({ x: 0, y: 0, zoom: 1 });
  const [nodePositions, setNodePositions] = useState<Record<string, Point>>({});
  const [nodeSizes, setNodeSizes] = useState<Record<string, NodeSize>>({});

  const [island, setIsland] = useState({ visible: false, x: 20, y: 20, text: "" });
  const [islandHeight, setIslandHeight] = useState(ISLAND_MIN_HEIGHT);

  const wrapperRef = useRef<HTMLDivElement | null>(null);
  const nodeElementRefs = useRef<Record<string, HTMLDivElement | null>>({});
  const caseSelectorRef = useRef<HTMLDivElement | null>(null);
  const hiddenFileInputRef = useRef<HTMLInputElement | null>(null);
  const uploadAbortRef = useRef<AbortController | null>(null);
  const progressTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const selectionSyncTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const viewportSyncTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const displayLevelSyncTimersRef = useRef<Record<string, ReturnType<typeof setTimeout>>>({});
  const pendingDisplayLevelsRef = useRef<Record<string, 1 | 2 | 3>>({});
  const workspaceMutationQueueRef = useRef<Promise<void>>(Promise.resolve());
  const canvasWheelHandlerRef = useRef<(event: WheelEvent) => void>(() => undefined);
  const quoteRequestRef = useRef(0);

  const panStateRef = useRef<{ active: boolean; startMouseX: number; startMouseY: number; startX: number; startY: number }>({
    active: false,
    startMouseX: 0,
    startMouseY: 0,
    startX: 0,
    startY: 0,
  });
  const nodeDragRef = useRef<{
    id: string;
    startMouseX: number;
    startMouseY: number;
    startX: number;
    startY: number;
    zoom: number;
  } | null>(null);
  const islandDragRef = useRef<{ offsetX: number; offsetY: number } | null>(null);
  const transformRef = useRef(transform);
  const pendingDragRef = useRef<{ id: string; x: number; y: number } | null>(null);
  const dragRafRef = useRef<number | null>(null);
  const pendingPanRef = useRef<{ x: number; y: number } | null>(null);
  const panRafRef = useRef<number | null>(null);
  const dragPreviewPointRef = useRef<Point | null>(null);
  const islandContainerRef = useRef<HTMLDivElement | null>(null);
  const islandInputRef = useRef<HTMLTextAreaElement | null>(null);
  const islandHeightRef = useRef(ISLAND_MIN_HEIGHT);

  const selectedInfoUnit = useMemo(
    () => currentCase?.info_units.find((unit) => unit.id === focusedNodeId) ?? null,
    [currentCase, focusedNodeId],
  );

  const editedInfoUnit = useMemo(
    () => infoUnitEditor?.infoUnitId
      ? currentCase?.info_units.find((unit) => unit.id === infoUnitEditor.infoUnitId) ?? null
      : null,
    [currentCase, infoUnitEditor],
  );

  const filteredQuoteUnits = useMemo(() => {
    const keyword = quoteSearch.trim().toLowerCase();
    return quoteUnits.filter((unit) => {
      if (unit.role === "group_boundary") return false;
      if (!keyword) return true;
      const file = currentCase?.files.find((item) => item.file_id === unit.file_id);
      return unit.text.toLowerCase().includes(keyword) || file?.filename.toLowerCase().includes(keyword) === true;
    });
  }, [currentCase, quoteSearch, quoteUnits]);

  useEffect(() => {
    transformRef.current = transform;
  }, [transform]);

  useEffect(() => () => {
    if (selectionSyncTimerRef.current) clearTimeout(selectionSyncTimerRef.current);
    if (viewportSyncTimerRef.current) clearTimeout(viewportSyncTimerRef.current);
    Object.values(displayLevelSyncTimersRef.current).forEach((timer) => clearTimeout(timer));
  }, []);

  useEffect(() => {
    const wrapper = wrapperRef.current;
    if (!wrapper) return;
    const handleWheel = (event: WheelEvent) => canvasWheelHandlerRef.current(event);
    wrapper.addEventListener("wheel", handleWheel, { passive: false });
    return () => wrapper.removeEventListener("wheel", handleWheel);
  }, []);

  useEffect(() => {
    islandHeightRef.current = islandHeight;
  }, [islandHeight]);

  useEffect(() => {
    const input = islandInputRef.current;
    if (!input) return;
    input.style.height = "auto";
    const nextHeight = Math.min(ISLAND_MAX_INPUT_HEIGHT, Math.max(20, input.scrollHeight));
    input.style.height = `${nextHeight}px`;
  }, [island.text, island.visible]);

  useEffect(() => {
    const islandEl = islandContainerRef.current;
    if (!islandEl || !island.visible) return;
    const nextHeight = Math.max(ISLAND_MIN_HEIGHT, islandEl.offsetHeight);
    if (nextHeight !== islandHeightRef.current) {
      setIslandHeight(nextHeight);
    }
  }, [island.text, island.visible]);

  function pickSelectedNodeIds(nextCase: CaseData | null) {
    if (!nextCase) return [] as string[];
    const ids = nextCase.workspace_state?.selected_card_ids ?? [];
    if (ids.length > 0) return ids;
    const focused = nextCase.workspace_state?.focused_card_id;
    return focused ? [focused] : [];
  }

  function pickFocusedNodeId(nextCase: CaseData | null) {
    if (!nextCase) return null;
    return nextCase.workspace_state?.focused_card_id ?? pickSelectedNodeIds(nextCase)[0] ?? null;
  }

  function getNodeVisualLevel(node: CanvasNode): 1 | 2 | 3 {
    if (node.source === "evidence") return 1;
    if (node.source === "placeholder") return 2;
    return node.level;
  }

  function getNodeHeaderKind(node: CanvasNode): string {
    if (node.semanticRole === "conflict") return "冲突";
    if (node.source === "meta") return node.actionLabel || "";
    if (node.source === "inference") return "推论";
    if (node.source === "law") return "法条";
    if (node.source === "evidence") return "证据";
    return "";
  }

  useEffect(() => {
    void (async () => {
      try {
        const [current, cases] = await Promise.all([getCase(), listCases()]);
        setCurrentCase(current.case);
        setCaseOptions(cases.cases);

        if (current.case?.case_id) {
          setSelectedCaseId(current.case.case_id);
          setSelectedNodeIds(pickSelectedNodeIds(current.case));
          setFocusedNodeId(pickFocusedNodeId(current.case));
          const viewport = current.case.workspace_state?.viewport;
          if (viewport) {
            setTransform(clampViewportToWrapper({
              x: viewport.offset_x,
              y: viewport.offset_y,
              zoom: clampZoom(viewport.zoom),
            }, wrapperRef.current));
          }
          return;
        }

        const selected = cases.cases.find((item) => item.is_current) ?? cases.cases[0];
        if (selected) {
          setSelectedCaseId(selected.case_id);
        }
      } catch {
        // ignore initial load failures
      }
    })();
  }, []);

  useEffect(() => {
    if (selectedNodeIds.length === 0) {
      setFocusedNodeId(currentCase?.workspace_state?.focused_card_id ?? null);
      return;
    }

    setFocusedNodeId((current) => {
      if (current && selectedNodeIds.includes(current)) return current;
      return selectedNodeIds[selectedNodeIds.length - 1] ?? null;
    });
  }, [currentCase?.workspace_state?.focused_card_id, selectedNodeIds]);

  useEffect(() => {
    const clampCurrentViewport = () => {
      setTransform((current) => clampViewportToWrapper(current, wrapperRef.current));
    };

    clampCurrentViewport();
    window.addEventListener("resize", clampCurrentViewport);
    return () => window.removeEventListener("resize", clampCurrentViewport);
  }, []);

  useEffect(() => {
    const onWindowClick = (event: MouseEvent) => {
      if (!caseSelectorRef.current) return;
      if (caseSelectorRef.current.contains(event.target as Node)) return;
      setCaseMenuOpen(false);
    };
    window.addEventListener("click", onWindowClick);
    return () => window.removeEventListener("click", onWindowClick);
  }, []);

  const allFiles = useMemo<FileTreeItem[]>(() => {
    const items: FileTreeItem[] = [];

    evidences.forEach((item) => {
      items.push({
        id: item.id,
        name: item.file.name,
        type: item.type === "image" ? "image" : "document",
        status: evidenceStatusLabel(item.status),
        source: "queued",
        subText: `${formatFileDate(item.file.lastModified)} | ${readableBytes(item.file.size)}`,
      });
    });

    (currentCase?.files ?? []).forEach((file) => {
      items.push({
        id: file.file_id,
        name: file.filename,
        type: file.file_type === "image" ? "image" : "document",
        status: parseStatusLabel(file.parse_status),
        source: "parsed",
        subText: `${formatFileDate(file.uploaded_at)} | ${readableBytes(file.file_size ?? 0)}`,
      });
    });

    const keyword = searchKeyword.trim().toLowerCase();
    if (!keyword) return items;
    return items.filter((item) => item.name.toLowerCase().includes(keyword));
  }, [evidences, currentCase, searchKeyword]);

    const canvasData = useMemo(() => {
    if (!currentCase) {
      return { nodes: [] as CanvasNode[], edges: [] as CanvasEdge[] };
    }

    const nodes: CanvasNode[] = [];
    const edges: CanvasEdge[] = [];

    currentCase.meta_cards.slice(0, 24).forEach((card, index) => {
      const matchedInfoUnit = findMatchedInfoUnit(currentCase, card);
      const description = card.detail.description || card.summary || card.title;
      nodes.push({
        id: card.id,
        label: card.title,
        meta: card.summary || card.detail.description || card.info_subtype || card.info_type,
        actionLabel: infoTypeLabel(card.info_type),
        detailFields: buildDetailFields([
          ["subType", card.info_subtype],
          ["description", description],
          ["speaker", matchedInfoUnit?.speaker ?? null],
          ["side", matchedInfoUnit?.side ?? null],
          ["actor", matchedInfoUnit?.actor ?? null],
          ["target", matchedInfoUnit?.target ?? null],
          ["relatedIssue", matchedInfoUnit?.related_issue ?? null],
          ["sourceQuotes", matchedInfoUnit?.source_refs.map((ref) => ref.quote) ?? []],
        ]),
        kind: kindFromInfoType(card.info_type),
        source: "meta",
        level: card.display_level,
        state: "normal",
        x: card.position?.x ?? (120 + (index % 4) * 280),
        y: card.position?.y ?? (120 + Math.floor(index / 4) * 170),
      });
    });

    currentCase.inference_cards.slice(0, 16).forEach((card, index) => {
      const isConflictCard = card.inference_type === "conflict";
      const conflictDetail = isConflictCard ? buildConflictDetail(currentCase, card) : undefined;
      nodes.push({
        id: card.id,
        label: card.title,
        meta: card.detail.claim,
        actionLabel: isConflictCard ? "冲突" : "推论",
        detailFields: buildDetailFields([
          ["ID", card.id],
          ["InferenceType", card.inference_type],
          ["Decision", card.decision],
          ["Claim", card.detail.claim],
          ["ReasoningSteps", card.detail.reasoning_steps],
          ["Supporting", card.detail.supporting_card_ids],
          ["Opposing", card.detail.opposing_card_ids],
          ["Missing", card.detail.missing_card_ids],
          ["Confidence", card.detail.confidence],
                  ]),
        kind: "claim",
        source: "inference",
        level: card.display_level,
        state: conflictDetail?.state === "muted" ? "missing" : "normal",
        semanticRole: isConflictCard ? "conflict" : "default",
        conflictDetail,
        x: card.position?.x ?? (1320 + (index % 2) * 300),
        y: card.position?.y ?? (160 + Math.floor(index / 2) * 180),
      });
    });

    currentCase.law_cards.slice(0, 12).forEach((card, index) => {
      nodes.push({
        id: card.id,
        label: card.title,
        meta: card.detail.title_full || card.summary || `${card.source_type} / ${card.norm_level}`,
        actionLabel: "法条",
        detailFields: buildDetailFields([
          ["ID", card.id],
          ["SourceType", card.source_type],
          ["NormLevel", card.norm_level],
          ["ArticleNo", card.detail.article_no],
          ["TitleFull", card.detail.title_full],
          ["EffectiveStatus", card.detail.effective_status],
          ["SourceUrl", card.detail.source_url],
          ["Keywords", card.detail.keywords],
          ["Text", card.detail.text],
          ["Summary", card.summary],
        ]),
        kind: "law",
        source: "law",
        level: card.display_level,
        state: "normal",
        x: card.position?.x ?? (1880 + (index % 2) * 300),
        y: card.position?.y ?? (140 + Math.floor(index / 2) * 180),
      });
    });

    const existingNodeIds = new Set(nodes.map((node) => node.id));
    const missingEvidenceIds = new Set<string>();
    currentCase.inference_cards.forEach((card) => {
      (card.detail?.missing_card_ids ?? []).forEach((missingId) => {
        const normalized = missingId.trim();
        if (!normalized) return;
        if (existingNodeIds.has(normalized)) return;
        missingEvidenceIds.add(normalized);
      });
    });

    Array.from(missingEvidenceIds)
      .slice(0, 18)
      .forEach((missingId, index) => {
        nodes.push({
          id: `missing-${missingId}`,
          label: "待补充",
          meta: `缺失证据: ${missingId}`,
          detailFields: buildDetailFields([
            ["Type", "待补充"],
            ["MissingTarget", missingId],
          ]),
          kind: "object",
          source: "placeholder",
          level: 2,
          state: "missing",
          x: 440 + (index % 2) * 460,
          y: 620 + Math.floor(index / 2) * 164,
        });
      });

    evidences.slice(0, 12).forEach((item, index) => {
      nodes.push({
        id: `queued-${item.id}`,
        label: item.file.name,
        meta: item.type,
        actionLabel: "证据",
        detailFields: buildDetailFields([
          ["ID", item.id],
          ["File", item.file.name],
          ["MimeType", item.file.type],
          ["Size", readableBytes(item.file.size)],
          ["Status", item.status],
        ]),
        kind: "evidence",
        source: "evidence",
        level: 1,
        state: "normal",
        x: 120 + (index % 3) * 280,
        y: 1180 + Math.floor(index / 3) * 160,
      });
    });

    currentCase.edges.slice(0, 60).forEach((edge) => {
      edges.push({
        id: edge.id,
        sourceId: edge.source,
        targetId: edge.target,
        relationType: edgeTypeLabel(edge.edge_type, edge.relation_type || edge.label),
        strength: edgeStrength(edge.edge_type),
      });
    });

    return { nodes, edges };
  }, [currentCase, evidences]);

  const canvasNodeIdsKey = canvasData.nodes.map((node) => node.id).join("\u0000");
  const canvasNodeLayoutKey = canvasData.nodes.map((node) => `${node.id}:${node.level}`).join("\u0000");

  useEffect(() => {
    if (canvasData.nodes.length === 0) {
      setNodePositions({});
      return;
    }

    setNodePositions((previous) => {
      const next: Record<string, Point> = { ...previous };
      let changed = false;
      canvasData.nodes.forEach((node) => {
        if (!next[node.id]) {
          next[node.id] = { x: node.x, y: node.y };
          changed = true;
        }
      });
      Object.keys(next).forEach((id) => {
        if (!canvasData.nodes.some((node) => node.id === id)) {
          delete next[id];
          changed = true;
        }
      });
      return changed ? next : previous;
    });
  }, [canvasNodeIdsKey]);

  useEffect(() => {
    const handleMove = (event: MouseEvent) => {
      const draggingNode = nodeDragRef.current;
      if (draggingNode) {
        const dx = (event.clientX - draggingNode.startMouseX) / draggingNode.zoom;
        const dy = (event.clientY - draggingNode.startMouseY) / draggingNode.zoom;
        pendingDragRef.current = { id: draggingNode.id, x: draggingNode.startX + dx, y: draggingNode.startY + dy };

        if (dragRafRef.current === null) {
          dragRafRef.current = window.requestAnimationFrame(() => {
            dragRafRef.current = null;
            const pending = pendingDragRef.current;
            if (!pending) return;
            const nextPoint = { x: pending.x, y: pending.y };
            dragPreviewPointRef.current = nextPoint;
            setNodePositions((previous) => {
              const current = previous[pending.id];
              if (current && current.x === nextPoint.x && current.y === nextPoint.y) {
                return previous;
              }
              return {
                ...previous,
                [pending.id]: nextPoint,
              };
            });
          });
        }
        return;
      }

      const draggingIsland = islandDragRef.current;
      if (draggingIsland) {
        const wrapper = wrapperRef.current;
        if (!wrapper) return;
        const rect = wrapper.getBoundingClientRect();
        const rawX = event.clientX - rect.left - draggingIsland.offsetX;
        const rawY = event.clientY - rect.top - draggingIsland.offsetY;
        const clampedX = Math.max(ISLAND_PADDING, Math.min(rawX, rect.width - ISLAND_WIDTH - ISLAND_PADDING));
        const currentIslandHeight = islandContainerRef.current?.offsetHeight ?? islandHeightRef.current;
        const clampedY = Math.max(ISLAND_PADDING, Math.min(rawY, rect.height - currentIslandHeight - ISLAND_PADDING));
        setIsland((current) => ({ ...current, x: clampedX, y: clampedY }));
        return;
      }

      const pan = panStateRef.current;
      if (!pan.active) return;
      const dx = event.clientX - pan.startMouseX;
      const dy = event.clientY - pan.startMouseY;
      pendingPanRef.current = clampViewportToWrapper({
        x: pan.startX + dx,
        y: pan.startY + dy,
        zoom: transformRef.current.zoom,
      }, wrapperRef.current);

      if (panRafRef.current === null) {
        panRafRef.current = window.requestAnimationFrame(() => {
          panRafRef.current = null;
          const pending = pendingPanRef.current;
          if (!pending) return;
          transformRef.current = { ...transformRef.current, x: pending.x, y: pending.y };
          setTransform(transformRef.current);
        });
      }
    };

    const handleUp = () => {
      const draggedNode = nodeDragRef.current;
      const wasPanning = panStateRef.current.active;
      let finalPanTransform = transformRef.current;

      if (islandDragRef.current) {
        const wrapper = wrapperRef.current;
        if (wrapper) {
          const rect = wrapper.getBoundingClientRect();
          setIsland((current) => {
            if (!current.visible) return current;
            const currentIslandHeight = islandContainerRef.current?.offsetHeight ?? islandHeightRef.current;
            const bottomGap = rect.height - (current.y + currentIslandHeight);
            if (bottomGap <= ISLAND_BOTTOM_DOCK_THRESHOLD) {
              const dockX = Math.max(ISLAND_PADDING, (rect.width - ISLAND_WIDTH) / 2);
              const dockY = Math.max(ISLAND_PADDING, rect.height - currentIslandHeight - ISLAND_PADDING);
              return { ...current, x: dockX, y: dockY };
            }
            return current;
          });
        }
      }

      islandDragRef.current = null;
      panStateRef.current.active = false;
      nodeDragRef.current = null;

      if (dragRafRef.current !== null) {
        window.cancelAnimationFrame(dragRafRef.current);
        dragRafRef.current = null;
      }
      if (panRafRef.current !== null) {
        window.cancelAnimationFrame(panRafRef.current);
        panRafRef.current = null;
      }
      if (wasPanning && pendingPanRef.current) {
        finalPanTransform = {
          ...transformRef.current,
          x: pendingPanRef.current.x,
          y: pendingPanRef.current.y,
        };
        transformRef.current = finalPanTransform;
        setTransform(finalPanTransform);
      }

      if (draggedNode) {
        const pending = pendingDragRef.current;
        const finalPoint = pending
          ? { x: pending.x, y: pending.y }
          : dragPreviewPointRef.current ?? nodePositions[draggedNode.id];

        if (finalPoint) {
          setNodePositions((previous) => ({
            ...previous,
            [draggedNode.id]: finalPoint,
          }));
          void persistCardPosition(draggedNode.id, finalPoint);
        }
      }

      setDraggingNodeId(null);
      pendingDragRef.current = null;
      pendingPanRef.current = null;
      dragPreviewPointRef.current = null;

      if (wasPanning) {
        void persistWorkspace(finalPanTransform);
      }
    };

    window.addEventListener("mousemove", handleMove);
    window.addEventListener("mouseup", handleUp);
    return () => {
      window.removeEventListener("mousemove", handleMove);
      window.removeEventListener("mouseup", handleUp);
      if (dragRafRef.current !== null) {
        window.cancelAnimationFrame(dragRafRef.current);
        dragRafRef.current = null;
      }
      if (panRafRef.current !== null) {
        window.cancelAnimationFrame(panRafRef.current);
        panRafRef.current = null;
      }
    };
  }, [nodePositions, currentCase]);

  function addDroppedFiles(fileList: FileList | File[]) {
    const list = Array.from(fileList);
    if (list.length === 0) return;

    let nextFiles: File[] = [];
    setEvidences((current) => {
      const next = [
        ...current,
        ...list.map((file) => ({
          id: `evidence-${Date.now()}-${file.name}`,
          status: "pending" as const,
          type: inferType(file),
          file,
        })),
      ];
      nextFiles = next.map((item) => item.file);
      return next;
    });

    setError("");
    setIsDropSelected(true);
    setTimeout(() => {
      void submitCase(nextFiles);
    }, 0);
  }

  function onDropUpload(event: React.DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setIsDropActive(false);
    if (event.dataTransfer.files?.length) {
      addDroppedFiles(event.dataTransfer.files);
    }
  }

  function removeEvidence(id: string) {
    setEvidences((current) => current.filter((item) => item.id !== id));
  }

  function stopProgressTimer() {
    if (progressTimerRef.current) {
      clearInterval(progressTimerRef.current);
      progressTimerRef.current = null;
    }
  }

  async function submitCase(filesOverride?: File[]) {
    const files = filesOverride ?? evidences.map((item) => item.file);

    if (files.length === 0) {
      setError("Please add at least one file.");
      return;
    }

    setCaseLoading(true);
    setUploadStage("uploading");
    setUploadDisplayText(formatUploadFileNames(files));
    setError("");
    setUploadProgress(3);

    stopProgressTimer();
    progressTimerRef.current = setInterval(() => {
      setUploadProgress((current) => (current >= 90 ? current : current + 3));
    }, 180);

    const controller = new AbortController();
    uploadAbortRef.current = controller;

    setEvidences((current) => current.map((item) => ({ ...item, status: "submitted" as const })));

    try {
      const response = currentCase?.case_id
        ? await appendFilesToCase(currentCase.case_id, files, controller.signal)
        : await createCase(currentCase?.case_title ?? "未命名案件", files, controller.signal);
      setUploadProgress(100);
      setCurrentCase(response.case);
      closeInfoUnitEditor();
      setSelectedNodeIds(pickSelectedNodeIds(response.case));
      setFocusedNodeId(pickFocusedNodeId(response.case));
      if (response.case.workspace_state?.viewport) {
        const viewport = response.case.workspace_state.viewport;
        setTransform(clampViewportToWrapper({
          x: viewport.offset_x,
          y: viewport.offset_y,
          zoom: clampZoom(viewport.zoom),
        }, wrapperRef.current));
      }
      setEvidences([]);
      setUploadStage("idle");
      try {
        const cases = await listCases();
        setCaseOptions(cases.cases);
        setSelectedCaseId(response.case.case_id);
      } catch {
        // ignore refresh failures
      }
    } catch (err) {
      if (!controller.signal.aborted) {
        setEvidences((current) => current.map((item) => ({ ...item, status: "pending" as const })));
        setError(err instanceof Error ? err.message : "Unknown error");
        setUploadStage("idle");
      }
    } finally {
      stopProgressTimer();
      uploadAbortRef.current = null;
      setCaseLoading(false);
    }
  }

  async function onRunExtraction() {
    if (!currentCase) return;
    setExtractionRunning(true);
    setError("");
    try {
      await runExtraction({ case_id: currentCase.case_id });
      const refreshed = await getCaseById(currentCase.case_id);
      setCurrentCase(refreshed.case);
      setSelectedNodeIds(pickSelectedNodeIds(refreshed.case));
      setFocusedNodeId(pickFocusedNodeId(refreshed.case));
    } catch (err) {
      setError(`Extraction failed: ${err instanceof Error ? err.message : "Unknown error"}`);
    } finally {
      setExtractionRunning(false);
    }
  }

  function onCancelUpload() {
    uploadAbortRef.current?.abort();
    stopProgressTimer();
    setCaseLoading(false);
    setUploadProgress(0);
    setUploadStage("idle");
    setUploadDisplayText("");
    setEvidences((current) => current.map((item) => ({ ...item, status: "pending" as const })));
  }

  async function onClearCase() {
    uploadAbortRef.current?.abort();
    stopProgressTimer();
    setUploadProgress(0);
    setError("");
    await deleteCase();
    closeInfoUnitEditor();
    setCurrentCase(null);
    setSelectedNodeIds([]);
    setFocusedNodeId(null);
    setDraggingNodeId(null);
    setEvidences([]);
    setNodePositions({});
    setTransform(clampViewportToWrapper({ x: 0, y: 0, zoom: 1 }, wrapperRef.current));
    setUploadStage("idle");
    setUploadDisplayText("");
    try {
      const cases = await listCases();
      setCaseOptions(cases.cases);
      const selected = cases.cases.find((item) => item.is_current) ?? cases.cases[0];
      setSelectedCaseId(selected?.case_id ?? "");
    } catch {
      // ignore refresh failures
    }
  }

  function enqueueWorkspaceMutation(mutation: () => Promise<void>): Promise<void> {
    const queued = workspaceMutationQueueRef.current.catch(() => undefined).then(mutation);
    workspaceMutationQueueRef.current = queued.catch(() => undefined);
    return queued;
  }

  async function persistWorkspace(nextTransform: CanvasTransform) {
    if (!currentCase?.case_id) return;
    try {
      await enqueueWorkspaceMutation(async () => {
        await updateWorkspaceState(currentCase.case_id, {
          viewport: {
            zoom: nextTransform.zoom,
            offset_x: nextTransform.x,
            offset_y: nextTransform.y,
          },
        });
      });
    } catch {
      // keep local interaction smooth even if persistence fails
    }
  }

  function scheduleViewportSync(nextTransform: CanvasTransform) {
    if (viewportSyncTimerRef.current) clearTimeout(viewportSyncTimerRef.current);
    viewportSyncTimerRef.current = setTimeout(() => {
      void persistWorkspace(nextTransform);
    }, VIEWPORT_SAVE_DELAY_MS);
  }

  async function persistSelectedNodes(nodeIds: string[], focusedNodeId: string | null) {
    if (!currentCase?.case_id) return;
    const knownCardIds = new Set([
      ...currentCase.meta_cards.map((card) => card.id),
      ...currentCase.inference_cards.map((card) => card.id),
      ...currentCase.law_cards.map((card) => card.id),
    ]);
    const filteredIds = nodeIds.filter((id) => knownCardIds.has(id));
    const focused = focusedNodeId && knownCardIds.has(focusedNodeId) ? focusedNodeId : (filteredIds[0] ?? null);

    try {
      await enqueueWorkspaceMutation(async () => {
        await updateWorkspaceState(currentCase.case_id, {
          selected_card_ids: filteredIds,
          focused_card_id: focused,
        });
      });
    } catch {
      // keep local interaction smooth even if persistence fails
    }
  }

  function scheduleSelectionSync(nodeIds: string[], focusedNodeId: string | null) {
    if (selectionSyncTimerRef.current) {
      clearTimeout(selectionSyncTimerRef.current);
    }
    selectionSyncTimerRef.current = setTimeout(() => {
      void persistSelectedNodes(nodeIds, focusedNodeId);
    }, 120);
  }

  async function persistCardPosition(cardId: string, point: Point) {
    if (!currentCase?.case_id) return;
    const isKnownCard =
      currentCase.meta_cards.some((card) => card.id === cardId) ||
      currentCase.inference_cards.some((card) => card.id === cardId) ||
      currentCase.law_cards.some((card) => card.id === cardId);
    if (!isKnownCard) return;

    try {
      await enqueueWorkspaceMutation(async () => {
        await updateCardPosition(currentCase.case_id, cardId, point);
      });
    } catch {
      // keep local interaction smooth even if persistence fails
    }
  }

  async function persistCardDisplayLevel(cardId: string, displayLevel: 1 | 2 | 3) {
    if (!currentCase?.case_id) return;
    const isKnownCard =
      currentCase.meta_cards.some((card) => card.id === cardId) ||
      currentCase.inference_cards.some((card) => card.id === cardId) ||
      currentCase.law_cards.some((card) => card.id === cardId);
    if (!isKnownCard) return;

    try {
      await enqueueWorkspaceMutation(async () => {
        await updateCardDisplayLevel(currentCase.case_id, cardId, displayLevel);
      });
    } catch {
      // keep local interaction smooth even if persistence fails
    }
  }

  function updateCardDisplayLevelLocally(cardId: string, displayLevel: 1 | 2 | 3) {
    pendingDisplayLevelsRef.current[cardId] = displayLevel;
    setCurrentCase((existing) => {
      if (!existing) return existing;
      return {
        ...existing,
        meta_cards: existing.meta_cards.map((card) => (card.id === cardId ? { ...card, display_level: displayLevel } : card)),
        inference_cards: existing.inference_cards.map((card) => (card.id === cardId ? { ...card, display_level: displayLevel } : card)),
        law_cards: existing.law_cards.map((card) => (card.id === cardId ? { ...card, display_level: displayLevel } : card)),
        workspace_state: {
          ...existing.workspace_state,
          display_levels: {
            ...(existing.workspace_state.display_levels ?? {}),
            [cardId]: displayLevel,
          },
        },
      };
    });

    const currentTimer = displayLevelSyncTimersRef.current[cardId];
    if (currentTimer) clearTimeout(currentTimer);
    displayLevelSyncTimersRef.current[cardId] = setTimeout(() => {
      delete displayLevelSyncTimersRef.current[cardId];
      void persistCardDisplayLevel(cardId, displayLevel).finally(() => {
        if (pendingDisplayLevelsRef.current[cardId] === displayLevel) {
          delete pendingDisplayLevelsRef.current[cardId];
        }
      });
    }, DISPLAY_LEVEL_SAVE_DELAY_MS);
  }

  function closeInfoUnitEditor() {
    quoteRequestRef.current += 1;
    setInfoUnitEditor(null);
    setEditorError("");
    setQuoteSearch("");
    setQuoteUnits([]);
    setSelectedEvidenceIds([]);
    setEvidenceChanged(false);
    setQuotesLoading(false);
  }

  async function openInfoUnitEditor(unit: InfoUnit | null = null) {
    if (!currentCase) return;
    const requestId = quoteRequestRef.current + 1;
    quoteRequestRef.current = requestId;
    setInfoUnitEditor({ mode: unit ? "edit" : "create", infoUnitId: unit?.id ?? null });
    setInfoUnitDraftState(unit ? infoUnitDraft(unit) : emptyInfoUnitDraft());
    setSelectedEvidenceIds([]);
    setEvidenceChanged(false);
    setQuoteSearch("");
    setEditorError("");
    setQuotesLoading(true);
    try {
      const units = await listQuoteUnits(currentCase.case_id);
      if (quoteRequestRef.current !== requestId) return;
      setQuoteUnits(units);
      if (unit) {
        setSelectedEvidenceIds(units
          .filter((quoteUnit) => unit.source_refs.some((ref) => quoteMatchesSource(quoteUnit, ref)))
          .map((quoteUnit) => quoteUnit.quote_id));
      }
    } catch (err) {
      if (quoteRequestRef.current !== requestId) return;
      setEditorError(err instanceof Error ? err.message : "Quote Unit 列表加载失败");
    } finally {
      if (quoteRequestRef.current === requestId) setQuotesLoading(false);
    }
  }

  function changeInfoUnitType(type: InfoUnitType) {
    setInfoUnitDraftState((current) => {
      let subtype: InfoUnitSubtype | null = null;
      if (type === "entity") {
        subtype = current.subtype && ENTITY_SUBTYPES.includes(current.subtype) ? current.subtype : "person";
      } else if (type === "event") {
        subtype = current.subtype && EVENT_SUBTYPES.includes(current.subtype) ? current.subtype : null;
      }
      return { ...current, type, subtype };
    });
  }

  function toggleEvidence(quoteId: string) {
    setSelectedEvidenceIds((current) => current.includes(quoteId)
      ? current.filter((id) => id !== quoteId)
      : [...current, quoteId]);
    setEvidenceChanged(true);
  }

  function buildInfoUnitPayload(): InfoUnitMutationInput {
    return {
      type: infoUnitDraftState.type,
      subtype: infoUnitDraftState.subtype,
      title: infoUnitDraftState.title.trim(),
      content: infoUnitDraftState.content.trim(),
      speaker: nullableText(infoUnitDraftState.speaker),
      side: nullableText(infoUnitDraftState.side),
      actor: nullableText(infoUnitDraftState.actor),
      target: nullableText(infoUnitDraftState.target),
      related_issue: nullableText(infoUnitDraftState.relatedIssue),
    };
  }

  async function refreshAfterInfoUnitMutation(caseId: string, fallback: CaseData): Promise<CaseData> {
    try {
      return (await getCaseById(caseId)).case;
    } catch (err) {
      setError(`InfoUnit 已保存，但案件重新加载失败：${err instanceof Error ? err.message : "未知错误"}`);
      return fallback;
    }
  }

  async function saveInfoUnit() {
    if (!currentCase || !infoUnitEditor) return;
    setEditorError("");
    if (!infoUnitDraftState.title.trim() || !infoUnitDraftState.content.trim()) {
      setEditorError("title 和 content 不能为空");
      return;
    }
    if (infoUnitEditor.mode === "create" && selectedEvidenceIds.length === 0) {
      setEditorError("新建 InfoUnit 至少需要选择一条来源");
      return;
    }
    if (infoUnitEditor.mode === "edit" && evidenceChanged && selectedEvidenceIds.length === 0) {
      setEditorError("InfoUnit 至少需要保留一条来源");
      return;
    }

    setEditorBusy(true);
    try {
      const beforeIds = new Set(currentCase.info_units.map((unit) => unit.id));
      const basePayload = buildInfoUnitPayload();
      const result = infoUnitEditor.mode === "create"
        ? await createInfoUnit(currentCase.case_id, { ...basePayload, evidence_ids: selectedEvidenceIds })
        : await updateInfoUnit(currentCase.case_id, infoUnitEditor.infoUnitId!, {
            ...basePayload,
            ...(evidenceChanged ? { evidence_ids: selectedEvidenceIds } : {}),
          });
      const refreshed = await refreshAfterInfoUnitMutation(currentCase.case_id, result.case);
      setCurrentCase(refreshed);
      const savedId = infoUnitEditor.mode === "edit"
        ? infoUnitEditor.infoUnitId
        : refreshed.info_units.find((unit) => !beforeIds.has(unit.id))?.id ?? null;
      if (savedId) {
        setSelectedNodeIds([savedId]);
        setFocusedNodeId(savedId);
      }
      closeInfoUnitEditor();
    } catch (err) {
      setEditorError(err instanceof Error ? err.message : "InfoUnit 保存失败");
    } finally {
      setEditorBusy(false);
    }
  }

  async function removeEditedInfoUnit() {
    if (!currentCase || infoUnitEditor?.mode !== "edit" || !infoUnitEditor.infoUnitId) return;
    if (!window.confirm("确认删除该 InfoUnit？存在业务引用时服务端会拒绝删除。")) return;
    setEditorBusy(true);
    setEditorError("");
    try {
      const result = await deleteInfoUnit(currentCase.case_id, infoUnitEditor.infoUnitId);
      const refreshed = await refreshAfterInfoUnitMutation(currentCase.case_id, result.case);
      setCurrentCase(refreshed);
      setSelectedNodeIds(pickSelectedNodeIds(refreshed));
      setFocusedNodeId(pickFocusedNodeId(refreshed));
      closeInfoUnitEditor();
    } catch (err) {
      setEditorError(err instanceof Error ? err.message : "InfoUnit 删除失败");
    } finally {
      setEditorBusy(false);
    }
  }

  function onCanvasMouseDown(event: React.MouseEvent<HTMLDivElement>) {
    if (event.button !== 0) return;
    if ((event.target as HTMLElement).closest(".canvas-node") || (event.target as HTMLElement).closest(".dynamic-island")) {
      return;
    }
    if (island.visible) {
      setIsland((current) => ({ ...current, visible: false }));
    }
    event.preventDefault();

    const current = transformRef.current;
    panStateRef.current = {
      active: true,
      startMouseX: event.clientX,
      startMouseY: event.clientY,
      startX: current.x,
      startY: current.y,
    };
  }

  function onCanvasContextMenu(event: React.MouseEvent<HTMLDivElement>) {
    event.preventDefault();
    const wrapper = wrapperRef.current;
    if (!wrapper) return;
    const rect = wrapper.getBoundingClientRect();

    const rawX = event.clientX - rect.left + ISLAND_PADDING;
    const rawY = event.clientY - rect.top + ISLAND_PADDING;
    const clampedX = Math.max(ISLAND_PADDING, Math.min(rawX, rect.width - ISLAND_WIDTH - ISLAND_PADDING));
    const currentIslandHeight = islandContainerRef.current?.offsetHeight ?? islandHeightRef.current;
    const clampedY = Math.max(ISLAND_PADDING, Math.min(rawY, rect.height - currentIslandHeight - ISLAND_PADDING));

    setIsland((current) => ({ ...current, visible: true, x: clampedX, y: clampedY }));
  }

  function onIslandMouseDown(event: React.MouseEvent<HTMLDivElement>) {
    if (event.button !== 0) return;
    if ((event.target as HTMLElement).tagName.toLowerCase() === "input") return;
    event.stopPropagation();
    const wrapper = wrapperRef.current;
    if (!wrapper) return;
    const rect = wrapper.getBoundingClientRect();
    islandDragRef.current = {
      offsetX: event.clientX - rect.left - island.x,
      offsetY: event.clientY - rect.top - island.y,
    };
  }

  function onNodeMouseDown(event: React.MouseEvent<HTMLDivElement>, nodeId: string) {
    event.preventDefault();
    event.stopPropagation();
    const point = nodePositions[nodeId];
    if (!point) return;
    setDraggingNodeId(nodeId);
    dragPreviewPointRef.current = { x: point.x, y: point.y };
    nodeDragRef.current = {
      id: nodeId,
      startMouseX: event.clientX,
      startMouseY: event.clientY,
      startX: point.x,
      startY: point.y,
      zoom: transformRef.current.zoom,
    };
  }

  async function onGenerateInferenceFromIsland() {
    if (!currentCase) {
      setError("No active case.");
      return;
    }

    const prompt = island.text.trim();
    if (!prompt) {
      setError("Please enter a prompt.");
      return;
    }

    setCaseLoading(true);
    setError("");
    try {
      const knownCardIds = new Set([
        ...currentCase.meta_cards.map((card) => card.id),
        ...currentCase.inference_cards.map((card) => card.id),
        ...currentCase.law_cards.map((card) => card.id),
      ]);
      const selectedCardIds = selectedNodeIds.filter((id) => knownCardIds.has(id));
      const fallbackCardIds = currentCase.meta_cards.slice(0, 6).map((card) => card.id);
      const mode = inferGenerateModeFromPrompt(prompt);
      await generateInference(
        currentCase.case_id,
        prompt,
        mode,
        selectedCardIds.length > 0 ? selectedCardIds : fallbackCardIds,
      );
      const [nextCase, nextCases] = await Promise.all([getCase(), listCases()]);
      setCurrentCase(nextCase.case);
      setCaseOptions(nextCases.cases);
      setIsland((current) => ({ ...current, text: "" }));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Generate inference failed");
    } finally {
      setCaseLoading(false);
    }
  }

  async function onRenameCurrentCase() {
    if (!selectedCaseId) return;
    const currentTitle = caseOptions.find((item) => item.case_id === selectedCaseId)?.title ?? currentCase?.case_title ?? "";
    const nextTitle = window.prompt("Rename case", currentTitle)?.trim();
    if (!nextTitle || nextTitle === currentTitle) return;

    try {
      const updated = await renameCase(selectedCaseId, nextTitle);
      setCurrentCase(updated.case);
      const nextCases = await listCases();
      setCaseOptions(nextCases.cases);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Rename case failed");
    }
  }
  function applyCanvasTransform(nextTransform: CanvasTransform, persist = true) {
    const clamped = clampViewportToWrapper(nextTransform, wrapperRef.current);
    transformRef.current = clamped;
    setTransform(clamped);
    if (persist) scheduleViewportSync(clamped);
  }

  function fitAllNodes() {
    const wrapper = wrapperRef.current;
    if (!wrapper || canvasData.nodes.length === 0) return;
    const rect = wrapper.getBoundingClientRect();
    const bounds = canvasData.nodes.reduce((result, node) => {
      const point = nodePositions[node.id] ?? { x: node.x, y: node.y };
      const size = getNodeSize(node.id);
      return {
        minX: Math.min(result.minX, point.x),
        minY: Math.min(result.minY, point.y),
        maxX: Math.max(result.maxX, point.x + size.width),
        maxY: Math.max(result.maxY, point.y + size.height),
      };
    }, { minX: Infinity, minY: Infinity, maxX: -Infinity, maxY: -Infinity });
    const contentWidth = Math.max(1, bounds.maxX - bounds.minX);
    const contentHeight = Math.max(1, bounds.maxY - bounds.minY);
    const availableWidth = Math.max(1, rect.width - FIT_NODES_PADDING * 2);
    const availableHeight = Math.max(1, rect.height - FIT_NODES_PADDING * 2);
    const zoom = clampZoom(Math.min(availableWidth / contentWidth, availableHeight / contentHeight));
    const x = (rect.width - contentWidth * zoom) / 2 - bounds.minX * zoom;
    const y = (rect.height - contentHeight * zoom) / 2 - bounds.minY * zoom;
    applyCanvasTransform({ x, y, zoom });
  }

  function onCanvasWheel(event: WheelEvent) {
    event.preventDefault();
    if (event.ctrlKey) {
      const wrapper = wrapperRef.current;
      if (!wrapper) return;
      const rect = wrapper.getBoundingClientRect();
      const pointerX = event.clientX - rect.left;
      const pointerY = event.clientY - rect.top;
      const current = transformRef.current;
      const nextZoom = clampZoom(current.zoom * Math.exp(-event.deltaY * 0.0015));
      if (nextZoom === current.zoom) return;
      const worldX = (pointerX - current.x) / current.zoom;
      const worldY = (pointerY - current.y) / current.zoom;
      applyCanvasTransform({
        x: pointerX - worldX * nextZoom,
        y: pointerY - worldY * nextZoom,
        zoom: nextZoom,
      });
      return;
    }

    if (!currentCase || !focusedNodeId) return;

    const currentNode = canvasData.nodes.find((node) => node.id === focusedNodeId);
    if (!currentNode) return;
    if (currentNode.source === "evidence" || currentNode.source === "placeholder" || currentNode.source === "law") return;

    const currentLevel = pendingDisplayLevelsRef.current[focusedNodeId] ?? currentNode.level;
    const direction = event.deltaY < 0 ? "up" : "down";
    const nextLevel = stepDisplayLevel(currentLevel, direction);
    if (nextLevel === currentLevel) return;

    updateCardDisplayLevelLocally(focusedNodeId, nextLevel);
  }

  canvasWheelHandlerRef.current = onCanvasWheel;

  const draggingNode = useMemo(() => {
    if (!draggingNodeId) return null;
    return canvasData.nodes.find((node) => node.id === draggingNodeId) ?? null;
  }, [canvasData.nodes, draggingNodeId]);

  useEffect(() => {
    const activeNodeIds = new Set(canvasData.nodes.map((node) => node.id));
    setNodeSizes((current) => {
      let changed = false;
      const next: Record<string, NodeSize> = {};

      Object.entries(current).forEach(([id, size]) => {
        if (!activeNodeIds.has(id)) {
          changed = true;
          return;
        }
        next[id] = size;
      });

      return changed ? next : current;
    });
  }, [canvasNodeIdsKey]);

  useLayoutEffect(() => {
    setNodeSizes((current) => {
      let changed = false;
      const next = { ...current };
      canvasData.nodes.forEach((node) => {
        const element = nodeElementRefs.current[node.id];
        if (!element) return;
        const width = element.offsetWidth;
        const height = element.offsetHeight;
        const previous = current[node.id];
        if (previous?.width === width && previous.height === height) return;
        next[node.id] = { width, height };
        changed = true;
      });
      return changed ? next : current;
    });
  }, [canvasNodeLayoutKey]);

  useEffect(() => {
    if (typeof ResizeObserver === "undefined") return;

    const observer = new ResizeObserver((entries) => {
      setNodeSizes((current) => {
        let changed = false;
        const next = { ...current };

        entries.forEach((entry) => {
          const element = entry.target as HTMLDivElement;
          const nodeId = element.dataset.nodeId;
          if (!nodeId) return;

          const width = element.offsetWidth;
          const height = element.offsetHeight;
          const prev = current[nodeId];
          if (prev && prev.width === width && prev.height === height) return;

          next[nodeId] = { width, height };
          changed = true;
        });

        return changed ? next : current;
      });
    });

    const initialSizes: Record<string, NodeSize> = {};
    canvasData.nodes.forEach((node) => {
      const element = nodeElementRefs.current[node.id];
      if (!element) return;

      observer.observe(element);
      initialSizes[node.id] = { width: element.offsetWidth, height: element.offsetHeight };
    });
    setNodeSizes((current) => {
      let changed = false;
      const next = { ...current };
      Object.entries(initialSizes).forEach(([nodeId, size]) => {
        const previous = current[nodeId];
        if (previous?.width === size.width && previous.height === size.height) return;
        next[nodeId] = size;
        changed = true;
      });
      return changed ? next : current;
    });

    return () => observer.disconnect();
  }, [canvasNodeIdsKey]);

  function getNodeSize(nodeId: string): NodeSize {
    return nodeSizes[nodeId] ?? DEFAULT_NODE_SIZE;
  }

  const selectedCaseLabel = caseOptions.find((item) => item.case_id === selectedCaseId)?.title ?? currentCase?.case_title ?? "Case";
  const dropzoneClass = `upload-dropzone figma-dropzone ${isDropActive || isDropSelected ? "active" : ""} ${uploadStage !== "idle" ? "uploading" : ""}`;
  const canvasContentStyle = {
    width: `${CANVAS_WIDTH}px`,
    height: `${CANVAS_HEIGHT}px`,
    transform: `translate(${transform.x}px, ${transform.y}px) scale(${transform.zoom})`,
  } as CSSProperties;

  function renderConflictGlyph() {
    return (
      <span className="conflict-glyph" aria-hidden="true">
        <span className="conflict-glyph-branch conflict-glyph-branch-left" />
        <span className="conflict-glyph-branch conflict-glyph-branch-right" />
        <span className="conflict-glyph-dot conflict-glyph-dot-top" />
        <span className="conflict-glyph-dot conflict-glyph-dot-left" />
        <span className="conflict-glyph-dot conflict-glyph-dot-right" />
      </span>
    );
  }

  function renderConflictList(title: string, tone: "support" | "oppose" | "missing", items: ConflictReferenceItem[]) {
    return (
      <section className={`conflict-section conflict-section-${tone}`}>
        <div className="conflict-section-header">
          <span>{title}</span>
          <span>{items.length}</span>
        </div>
        {items.length === 0 ? (
          <p className="conflict-empty">{"暂无"}</p>
        ) : (
          <div className="conflict-reference-list">
            {items.map((item) => (
              <article className="conflict-reference-item" key={`${tone}-${item.id}`}>
                <p className="conflict-reference-title" title={item.title}>{item.title}</p>
                <p className="conflict-reference-meta" title={item.meta}>{item.meta}</p>
              </article>
            ))}
          </div>
        )}
      </section>
    );
  }

  function renderConflictContent(node: Pick<CanvasNode, "id" | "label" | "conflictDetail">, visualLevel: 1 | 2 | 3) {
    const detail = node.conflictDetail;
    if (!detail) return null;

    if (visualLevel === 1) {
      return (
        <div className="conflict-pill-shell">
          <span className="conflict-icon-badge">{renderConflictGlyph()}</span>
          <span className="conflict-pill-title">{"冲突"}</span>
        </div>
      );
    }

    if (visualLevel === 2) {
      return (
        <div className="node-content-group conflict-card-shell">
          <div className="conflict-card-head">
            <div className="conflict-card-title-row">
              <span className="conflict-icon-badge">{renderConflictGlyph()}</span>
              <div className="conflict-card-title-copy">
                <p className="conflict-card-title" title={node.label}>{node.label}</p>
                <p className="conflict-card-state">{detail.stateLabel}</p>
              </div>
            </div>
            <span className="conflict-chip">{detail.decisionLabel}</span>
          </div>
          <p className="conflict-card-claim" title={detail.claim}>{detail.claim}</p>
          <div className="conflict-metric-row">
            <span className="conflict-mini-chip">{"支持"} {detail.supportItems.length}</span>
            <span className="conflict-mini-chip">{"反证"} {detail.opposeItems.length}</span>
            <span className="conflict-mini-chip">{"缺口"} {detail.missingItems.length}</span>
            {detail.confidenceLabel ? <span className="conflict-mini-chip">{detail.confidenceLabel}</span> : null}
          </div>
        </div>
      );
    }

    return (
      <div className="node-content-group conflict-detail-shell">
        <div className="conflict-card-head">
          <div className="conflict-card-title-row">
            <span className="conflict-icon-badge">{renderConflictGlyph()}</span>
            <div className="conflict-card-title-copy">
              <p className="conflict-card-title" title={node.label}>{node.label}</p>
              <p className="conflict-card-state">{detail.stateLabel}</p>
            </div>
          </div>
          <span className="conflict-chip">{detail.decisionLabel}</span>
        </div>
        <div className="conflict-summary-panel">
          <p className="conflict-card-claim">{detail.claim}</p>
          <div className="conflict-metric-row">
            <span className="conflict-mini-chip">{"支持"} {detail.supportItems.length}</span>
            <span className="conflict-mini-chip">{"反证"} {detail.opposeItems.length}</span>
            <span className="conflict-mini-chip">{"缺口"} {detail.missingItems.length}</span>
            {detail.confidenceLabel ? <span className="conflict-mini-chip">{detail.confidenceLabel}</span> : null}
          </div>
        </div>
        <div className="conflict-compare-grid">
          {renderConflictList("支持证据", "support", detail.supportItems)}
          {renderConflictList("反向证据", "oppose", detail.opposeItems)}
        </div>
        {detail.missingItems.length > 0 ? renderConflictList("待补证据", "missing", detail.missingItems) : null}
        {detail.reasoningSteps.length > 0 ? (
          <section className="conflict-section conflict-section-neutral">
            <div className="conflict-section-header">
              <span>{"推理链"}</span>
              <span>{detail.reasoningSteps.length}</span>
            </div>
            <ol className="conflict-step-list">
              {detail.reasoningSteps.map((step, index) => (
                <li className="conflict-step-item" key={`${node.id}-step-${index}`}>{step}</li>
              ))}
            </ol>
          </section>
        ) : null}
      </div>
    );
  }

  function renderNodeHeader(
    node: Pick<CanvasNode, "id" | "label" | "source">,
    headerKind: string,
    isMeta: boolean,
    isPlaceholder: boolean,
    isInference: boolean,
    isLaw: boolean,
  ) {
    return (
      <div className="node-header">
        <div className="node-type-icon-wrap">
          <img className="node-type-icon" src={isMeta || isPlaceholder ? metaTypeDefaultIcon : isInference ? inferTypeDefaultIcon : isLaw ? inferTypeDefaultIcon : detailsIcon} alt={isMeta ? "meta" : isInference ? "inference" : isLaw ? "law" : "evidence"} />
        </div>
        <span className="node-title" title={node.label}>{node.label}</span>
        {!isLaw && headerKind ? <span className="node-kind" title={headerKind}>{headerKind}</span> : null}
        <span className="node-id">{node.id}</span>
      </div>
    );
  }

  function renderNodeContent(
    node: Pick<CanvasNode, "id" | "label" | "meta" | "detailFields" | "source" | "semanticRole" | "conflictDetail">,
    visualLevel: 1 | 2 | 3,
    headerKind: string,
    isMeta: boolean,
    isPlaceholder: boolean,
    isInference: boolean,
    isLaw: boolean,
  ) {
    if (node.semanticRole === "conflict") {
      return renderConflictContent(node, visualLevel);
    }

    if (isLaw) {
      return (
        <div className="node-content-group">
          {renderNodeHeader(node, headerKind, isMeta, isPlaceholder, isInference, isLaw)}
          <p className="node-meta">{node.meta || "-"}</p>
        </div>
      );
    }

    return (
      <div className="node-content-group">
        {renderNodeHeader(node, headerKind, isMeta, isPlaceholder, isInference, isLaw)}
        {visualLevel >= 2 ? <p className="node-meta">{node.meta || "-"}</p> : null}
        {visualLevel >= 3 && node.detailFields.length > 0 ? (
          <div className="node-preview">
            {node.detailFields.map((field, index) => (
              <p className="node-preview-row" key={`${node.id}-field-${index}`}>
                <strong>{field.key}</strong>: {field.value}
              </p>
            ))}
          </div>
        ) : null}
      </div>
    );
  }

  function renderInfoUnitEditor() {
    if (!infoUnitEditor || !currentCase) return null;
    const subtypeOptions = infoUnitDraftState.type === "entity"
      ? ENTITY_SUBTYPES
      : infoUnitDraftState.type === "event"
        ? EVENT_SUBTYPES
        : [];
    const entityOptions = currentCase.info_units.filter((unit) =>
      unit.type === "entity" && !(unit.id === infoUnitEditor.infoUnitId && infoUnitDraftState.type !== "entity"));
    const currentSources = editedInfoUnit?.source_refs ?? [];

    return (
      <div className="info-unit-editor-backdrop" role="presentation" onMouseDown={(event) => event.stopPropagation()}>
        <section className="info-unit-editor" role="dialog" aria-modal="true" aria-labelledby="info-unit-editor-title">
          <header className="info-unit-editor-header">
            <div>
              <p className="info-unit-editor-kicker">InfoUnit</p>
              <h2 id="info-unit-editor-title">{infoUnitEditor.mode === "create" ? "新建信息节点" : "编辑信息节点"}</h2>
              {editedInfoUnit ? <p className="info-unit-editor-id">{editedInfoUnit.id}</p> : null}
            </div>
            <button type="button" className="editor-close" onClick={closeInfoUnitEditor} disabled={editorBusy} aria-label="关闭编辑器">×</button>
          </header>

          <div className="info-unit-editor-body">
            <div className="info-unit-form-grid">
              <label className="editor-field editor-field-wide">
                <span>Title</span>
                <input value={infoUnitDraftState.title} onChange={(event) => setInfoUnitDraftState((current) => ({ ...current, title: event.target.value }))} />
              </label>
              <label className="editor-field editor-field-wide">
                <span>Content</span>
                <textarea rows={4} value={infoUnitDraftState.content} onChange={(event) => setInfoUnitDraftState((current) => ({ ...current, content: event.target.value }))} />
              </label>
              <label className="editor-field">
                <span>Type</span>
                <select value={infoUnitDraftState.type} onChange={(event) => changeInfoUnitType(event.target.value as InfoUnitType)}>
                  {INFO_UNIT_TYPES.map((type) => <option key={type} value={type}>{type}</option>)}
                </select>
              </label>
              <label className="editor-field">
                <span>Subtype</span>
                <select
                  value={infoUnitDraftState.subtype ?? ""}
                  disabled={subtypeOptions.length === 0}
                  onChange={(event) => setInfoUnitDraftState((current) => ({ ...current, subtype: event.target.value ? event.target.value as InfoUnitSubtype : null }))}
                >
                  <option value="">无</option>
                  {subtypeOptions.map((subtype) => <option key={subtype} value={subtype}>{subtype}</option>)}
                </select>
              </label>
              <label className="editor-field">
                <span>Speaker{infoUnitDraftState.type === "state" || infoUnitDraftState.type === "position" ? " *" : ""}</span>
                <input value={infoUnitDraftState.speaker} onChange={(event) => setInfoUnitDraftState((current) => ({ ...current, speaker: event.target.value }))} />
              </label>
              <label className="editor-field">
                <span>Side</span>
                <input value={infoUnitDraftState.side} onChange={(event) => setInfoUnitDraftState((current) => ({ ...current, side: event.target.value }))} />
              </label>
              <label className="editor-field">
                <span>Actor</span>
                <select value={infoUnitDraftState.actor} onChange={(event) => setInfoUnitDraftState((current) => ({ ...current, actor: event.target.value }))}>
                  <option value="">无</option>
                  {entityOptions.map((unit) => <option key={unit.id} value={unit.id}>{unit.title} · {unit.id}</option>)}
                </select>
              </label>
              <label className="editor-field">
                <span>Target</span>
                <select value={infoUnitDraftState.target} onChange={(event) => setInfoUnitDraftState((current) => ({ ...current, target: event.target.value }))}>
                  <option value="">无</option>
                  {entityOptions.map((unit) => <option key={unit.id} value={unit.id}>{unit.title} · {unit.id}</option>)}
                </select>
              </label>
              <label className="editor-field editor-field-wide">
                <span>Related issue</span>
                <select value={infoUnitDraftState.relatedIssue} onChange={(event) => setInfoUnitDraftState((current) => ({ ...current, relatedIssue: event.target.value }))}>
                  <option value="">无</option>
                  {currentCase.issue_set.map((issue) => <option key={issue} value={issue}>{issue}</option>)}
                </select>
              </label>
            </div>

            <section className="source-editor-section">
              <div className="source-editor-heading">
                <div>
                  <h3>当前 SourceRefs</h3>
                  <p>来源只由后端根据 Quote Unit 生成；下列内容为已保存引用。</p>
                </div>
                <span>{currentSources.length}</span>
              </div>
              {currentSources.length ? (
                <div className="current-source-list">
                  {currentSources.map((ref, index) => {
                    const file = currentCase.files.find((item) => item.file_id === ref.file_id);
                    return (
                      <article className="current-source-item" key={`${ref.file_id}-${ref.start}-${ref.end}-${index}`}>
                        <small>{file?.filename ?? ref.file_id} · {ref.source_kind} · {ref.start}–{ref.end}</small>
                        <p>{ref.quote}</p>
                      </article>
                    );
                  })}
                </div>
              ) : <p className="editor-empty">新建节点尚无已保存来源。</p>}
            </section>

            <section className="source-editor-section">
              <div className="source-editor-heading">
                <div>
                  <h3>Quote Unit 证据</h3>
                  <p>{infoUnitEditor.mode === "edit" && !evidenceChanged ? "未调整时保存会保留现有 SourceRefs。" : `将提交 ${selectedEvidenceIds.length} 条证据。`}</p>
                </div>
                <span>{selectedEvidenceIds.length}</span>
              </div>
              <input className="quote-search" value={quoteSearch} onChange={(event) => setQuoteSearch(event.target.value)} placeholder="按文件名或原文筛选" />
              <div className="quote-unit-list">
                {quotesLoading ? <p className="editor-empty">正在加载 Quote Units…</p> : null}
                {!quotesLoading && filteredQuoteUnits.length === 0 ? <p className="editor-empty">没有可选择的 Quote Unit。</p> : null}
                {filteredQuoteUnits.map((unit) => {
                  const file = currentCase.files.find((item) => item.file_id === unit.file_id);
                  const checked = selectedEvidenceIds.includes(unit.quote_id);
                  return (
                    <label className={`quote-unit-option ${checked ? "selected" : ""}`} key={unit.quote_id}>
                      <input type="checkbox" checked={checked} onChange={() => toggleEvidence(unit.quote_id)} />
                      <span>
                        <small>{file?.filename ?? unit.file_id} · {file?.source_kind ?? "unknown"} · {unit.start}–{unit.end} · {unit.role}</small>
                        <strong>{unit.text}</strong>
                      </span>
                    </label>
                  );
                })}
              </div>
            </section>
          </div>

          {editorError ? <p className="info-unit-editor-error" role="alert">{editorError}</p> : null}
          <footer className="info-unit-editor-actions">
            {infoUnitEditor.mode === "edit" ? (
              <button type="button" className="editor-delete" onClick={() => void removeEditedInfoUnit()} disabled={editorBusy}>删除</button>
            ) : <span />}
            <div>
              <button type="button" className="editor-secondary" onClick={closeInfoUnitEditor} disabled={editorBusy}>取消</button>
              <button type="button" onClick={() => void saveInfoUnit()} disabled={editorBusy || quotesLoading}>{editorBusy ? "保存中…" : "保存"}</button>
            </div>
          </footer>
        </section>
      </div>
    );
  }

  return (
    <main className="page">
      <div className="studio-layout">
        <aside className="studio-sidebar figma-sidebar">
          <div className="sidebar-glow" />

          <div className="profile-row">
            <div className="user-avatar">
              <img src={myAvatar} alt="Sherlock Holmes" />
            </div>
            <div className="user-name">Sherlock Holmes</div>
            <div className="case-selector" ref={caseSelectorRef}>
              <button
                className="case-selector-trigger"
                type="button"
                title="Double click to rename"
                onClick={() => setCaseMenuOpen((current) => !current)}
                onDoubleClick={() => {
                  void onRenameCurrentCase();
                }}
              >
                <span>{selectedCaseLabel}</span>
                <img src={chevronsUpIcon} alt="case selector" className={`case-selector-chevron ${caseMenuOpen ? "open" : ""}`} />
              </button>
              {caseMenuOpen ? (
                <div className="case-selector-list">
                  <div className="case-selector-list-inner">
                    {caseOptions.map((item) => {
                      const selected = item.case_id === selectedCaseId;
                      return (
                        <button
                          key={item.case_id}
                          className={`case-selector-item ${selected ? "selected" : ""}`}
                          type="button"
                          onClick={() => {
                            void (async () => {
                              try {
                                const selectedCaseEnvelope = await selectCase(item.case_id);
                                setCurrentCase(selectedCaseEnvelope.case);
                                setSelectedNodeIds(pickSelectedNodeIds(selectedCaseEnvelope.case));
                                setFocusedNodeId(pickFocusedNodeId(selectedCaseEnvelope.case));
                                if (selectedCaseEnvelope.case?.workspace_state?.viewport) {
                                  const viewport = selectedCaseEnvelope.case.workspace_state.viewport;
                                  setTransform(clampViewportToWrapper({
                                    x: viewport.offset_x,
                                    y: viewport.offset_y,
                                    zoom: clampZoom(viewport.zoom),
                                  }, wrapperRef.current));
                                } else {
                                  setTransform(clampViewportToWrapper({ x: 0, y: 0, zoom: 1 }, wrapperRef.current));
                                }
                                setSelectedCaseId(item.case_id);
                                setEvidences([]);
                                closeInfoUnitEditor();
                                setCaseMenuOpen(false);
                              } catch (err) {
                                setError(err instanceof Error ? err.message : "Case switch failed");
                              }
                            })();
                          }}
                        >
                          <span>{item.title || "Untitled"}</span>
                          {selected ? <span className="case-selected-mark">v</span> : null}
                        </button>
                      );
                    })}
                  </div>
                </div>
              ) : null}
            </div>
          </div>

          <div className="search-row">
            <div className="search-box">
              <input value={searchKeyword} onChange={(event) => setSearchKeyword(event.target.value)} placeholder="Search..." />
              <img src={searchIcon} alt="search" className="search-icon" />
            </div>
            <button className="filter-btn" type="button" aria-label="filter">
              <img src={filterIcon} alt="filter" />
            </button>
          </div>

          <section className="folders-block">
            <div className="folder-header expanded">
              <img src={folderIcon} alt="folder" className="folder-icon" />
              <span>All Files</span>
              <img src={chevronsUpIcon} alt="expanded" className="chevron-icon" />
            </div>

            <div className="file-card-list">
              {allFiles.length === 0 ? <p className="tree-empty">No files</p> : null}
              {allFiles.map((item) => (
                <article className={`figma-file-card ${selectedFileId === item.id ? "is-selected" : ""}`} key={item.id} onClick={() => setSelectedFileId(item.id)}>
                  <div className="card-file-icon">
                    <img src={pickFileIcon(item.type, item.name)} alt={item.type} />
                  </div>
                  <div className="card-info">
                    <p className="card-title" title={item.name}>
                      {item.name}
                    </p>
                    <p className="card-sub">{item.subText}</p>
                  </div>
                  <div className="card-actions">
                    <span className={`status-badge status-${item.status === "submitted" ? "partial" : item.status}`}>{item.status}</span>
                    <button className="icon-btn" type="button" aria-label="details">
                      <img src={detailsIcon} alt="details" />
                    </button>
                    {item.source === "queued" ? (
                      <button className="danger tiny-btn" type="button" onClick={() => removeEvidence(item.id)}>
                        x
                      </button>
                    ) : null}
                  </div>
                </article>
              ))}
            </div>
          </section>

          <form className="upload-form-block">
            <div
              className={dropzoneClass}
              onDragEnter={(event) => {
                event.preventDefault();
                setIsDropActive(true);
              }}
              onDragOver={(event) => {
                event.preventDefault();
                setIsDropActive(true);
              }}
              onDragLeave={(event) => {
                event.preventDefault();
                setIsDropActive(false);
              }}
              onDrop={onDropUpload}
              onClick={() => {
                if (uploadStage === "idle") {
                  setIsDropSelected(true);
                  hiddenFileInputRef.current?.click();
                }
              }}
            >
              {uploadStage !== "idle" ? (
                <>
                  <div className="uploading-progress" style={{ background: `conic-gradient(#aea6ff ${uploadProgress * 3.6}deg, #303030 0deg)` }}>
                    <div className="uploading-progress-inner">{Math.round(uploadProgress)}%</div>
                  </div>
                  <div className="uploading-text-wrap">
                    <p title={uploadDisplayText}>{uploadDisplayText || "Uploaded files"}</p>
                    <button
                      className="uploading-cancel"
                      type="button"
                      onClick={(event) => {
                        event.stopPropagation();
                        onCancelUpload();
                      }}
                    >
                      Cancel
                    </button>
                  </div>
                </>
              ) : (
                <>
                  <img src={uploadIcon} alt="upload" className="upload-icon" />
                  <p>
                    Drag your file(s) or <span>browse</span>
                  </p>
                  <p className="drop-hint">Max 500 MB files are allowed</p>
                </>
              )}
            </div>

            <input
              ref={hiddenFileInputRef}
              type="file"
              multiple
              className="hidden-input"
              onChange={(event) => {
                if (event.target.files?.length) {
                  addDroppedFiles(event.target.files);
                }
                event.currentTarget.value = "";
              }}
            />
            {error ? <p className="error">request failed: {error}</p> : null}
          </form>
        </aside>

        <section className="panel studio-view">
          <div
            ref={wrapperRef}
            className="canvas-wrapper"
            onMouseDown={onCanvasMouseDown}
            onContextMenu={onCanvasContextMenu}
            style={{
              backgroundPosition: `${transform.x}px ${transform.y}px`,
              backgroundSize: `${42 * transform.zoom}px ${42 * transform.zoom}px`,
            }}
          >
            <div className="canvas-edit-toolbar" onMouseDown={(event) => event.stopPropagation()}>
              <button
                type="button"
                onClick={fitAllNodes}
                disabled={canvasData.nodes.length === 0}
                title="缩放并居中显示当前全部节点"
              >
                适应全部节点
              </button>
              <button
                type="button"
                onClick={() => void onRunExtraction()}
                disabled={!currentCase || extractionRunning || caseLoading}
                title="使用当前案件材料运行 Extraction"
              >
                {extractionRunning ? "提取中…" : "开始提取"}
              </button>
              <button type="button" onClick={() => void openInfoUnitEditor()} disabled={!currentCase}>+ 新建 InfoUnit</button>
              <button
                type="button"
                className="editor-secondary"
                onClick={() => void openInfoUnitEditor(selectedInfoUnit)}
                disabled={!selectedInfoUnit}
                title={selectedInfoUnit ? `编辑 ${selectedInfoUnit.title}` : "请先选中一个 InfoUnit 卡片"}
              >
                编辑选中节点
              </button>
            </div>
            <div className="canvas-zoom-badge" aria-live="polite">{Math.round(transform.zoom * 100)}%</div>
            <div
              className="canvas-content"
              style={canvasContentStyle}
            >
              <svg className="canvas-links" width={CANVAS_WIDTH} height={CANVAS_HEIGHT}>
                {canvasData.edges.map((edge) => {
                  const source = nodePositions[edge.sourceId];
                  const target = nodePositions[edge.targetId];
                  if (!source || !target) return null;

                  const sourceSize = getNodeSize(edge.sourceId);
                  const targetSize = getNodeSize(edge.targetId);

                  const sx = source.x + sourceSize.width / 2;
                  const sy = source.y + sourceSize.height / 2;
                  const tx = target.x + targetSize.width / 2;
                  const ty = target.y + targetSize.height / 2;
                  const mx = (sx + tx) / 2;
                  const my = (sy + ty) / 2;
                  const relationType = edge.relationType;
                  const labelWidth = Math.max(64, relationType.length * 9 + 20);
                  const labelHeight = 26;

                  return (
                    <g key={edge.id}>
                      <line x1={sx} y1={sy} x2={tx} y2={ty} className="canvas-link" strokeWidth={edge.strength} />
                      <rect
                        x={mx - labelWidth / 2}
                        y={my - labelHeight / 2}
                        width={labelWidth}
                        height={labelHeight}
                        rx={8}
                        ry={8}
                        className="edge-label-box"
                      />
                      <text x={mx} y={my} textAnchor="middle" dominantBaseline="central" className="edge-label-text">
                        {relationType}
                      </text>
                      <title>{relationType}</title>
                    </g>
                  );
                })}
              </svg>

              {canvasData.nodes.map((node) => {
                const point = nodePositions[node.id] ?? { x: node.x, y: node.y };
                const isMeta = node.source === "meta";
                const isInference = node.source === "inference";
                const isLaw = node.source === "law";
                const isPlaceholder = node.source === "placeholder";
                const visualLevel = getNodeVisualLevel(node);
                const levelClass = isMeta || isPlaceholder
                  ? `meta-level-${visualLevel}`
                  : isInference
                    ? `infer-level-${visualLevel}`
                    : isLaw
                      ? "law-level-2"
                      : "";
                const nodeClass = [
                  "canvas-node",
                  `node-${node.kind}`,
                  levelClass,
                  node.semanticRole === "conflict" ? "node-conflict" : "",
                  node.conflictDetail ? `conflict-state-${node.conflictDetail.state}` : "",
                  node.state === "missing" ? "node-missing" : "",
                  selectedNodeIds.includes(node.id) ? "node-selected" : "",
                ]
                  .filter(Boolean)
                  .join(" ");
                const headerKind = getNodeHeaderKind(node);
                return (
                  <div
                    key={node.id}
                    className={nodeClass}
                    data-node-id={node.id}
                    ref={(element) => {
                      nodeElementRefs.current[node.id] = element;
                    }}
                    style={{ left: point.x, top: point.y }}
                    onMouseDown={(event) => {
                      const isMultiSelect = event.metaKey || event.ctrlKey;
                      let nextSelectedNodeIds: string[] = [];

                      setSelectedNodeIds((current) => {
                        if (isMultiSelect) {
                          nextSelectedNodeIds = current.includes(node.id)
                            ? current.filter((id) => id !== node.id)
                            : [...current, node.id];
                          return nextSelectedNodeIds;
                        }
                        nextSelectedNodeIds = [node.id];
                        return nextSelectedNodeIds;
                      });

                      const nextFocusedNodeId = nextSelectedNodeIds.includes(node.id)
                        ? node.id
                        : (nextSelectedNodeIds[nextSelectedNodeIds.length - 1] ?? null);

                      setFocusedNodeId(nextFocusedNodeId);
                      scheduleSelectionSync(nextSelectedNodeIds, nextFocusedNodeId);
                      onNodeMouseDown(event, node.id);
                    }}
                  >
                    {renderNodeContent(node, visualLevel, headerKind, isMeta, isPlaceholder, isInference, isLaw)}
                  </div>
                );
              })}
            </div>

            {island.visible ? (
              <div
                ref={islandContainerRef}
                className="dynamic-island"
                style={{ left: island.x, top: island.y }}
                onMouseDown={onIslandMouseDown}
                onContextMenu={(event) => event.preventDefault()}
              >
                <img src={writeIcon} alt="write" className="island-icon" />
                <textarea
                  ref={islandInputRef}
                  value={island.text}
                  onChange={(event) => setIsland((current) => ({ ...current, text: event.target.value }))}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" && !event.shiftKey) {
                      event.preventDefault();
                      void onGenerateInferenceFromIsland();
                    }
                  }}
                  placeholder="Generate inference..."
                  rows={1}
                />

                <button
                  type="button"
                  className="icon-btn"
                  onClick={() => {
                    void onGenerateInferenceFromIsland();
                  }}
                  disabled={caseLoading}
                  aria-label="generate inference"
                >
                  <img src={sendIcon} alt="send" className="island-icon" />
                </button>
              </div>
            ) : null}

            {!currentCase ? <div className="canvas-empty">Upload files to generate nodes.</div> : null}
          </div>
          {renderInfoUnitEditor()}
        </section>
      </div>
    </main>
  );
}





















































































































































