import { useEffect, useMemo, useRef, useState } from "react";
import {
  createCase,
  appendFilesToCase,
  deleteCase,
  getCase,
  listCases,
  selectCase,
  generateInference,
  routeTask,
  runExtraction,
  runRelation,
  runReasoning,
  askQa,
  actInteraction,
  renameCase,
  updateCardPosition,
  updateWorkspaceState,
  trackInteraction,
  type CaseData,
  type CaseSummary,
  type MetaType,
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

type CanvasKind = "person" | "org" | "object" | "event" | "claim" | "evidence";

type CanvasNode = {
  id: string;
  label: string;
  meta: string;
  kind: CanvasKind;
  source: "meta" | "inference" | "evidence";
  level: 1 | 2 | 3;
  x: number;
  y: number;
};

type CanvasEdge = {
  id: string;
  sourceId: string;
  targetId: string;
  label: string;
  strength: number;
};

type Point = { x: number; y: number };

type FileTreeItem = {
  id: string;
  name: string;
  type: "document" | "image";
  status: string;
  source: "queued" | "parsed";
  subText: string;
};

const CANVAS_WIDTH = 3200;
const CANVAS_HEIGHT = 2200;
const NODE_WIDTH = 230;
const NODE_HEIGHT = 120;
const ISLAND_WIDTH = 422;
const ISLAND_HEIGHT = 38;
const ISLAND_PADDING = 8;
const ISLAND_BOTTOM_DOCK_THRESHOLD = 28;

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

function kindFromMetaType(type: MetaType): CanvasKind {
  if (type === "person") return "person";
  if (type === "organization") return "org";
  if (type === "event") return "event";
  if (type === "claim") return "claim";
  return "object";
}

function edgeStrength(edgeType: string) {
  if (edgeType === "supports") return 3;
  if (edgeType === "opposes" || edgeType === "conflicts_with") return 2;
  return 1;
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
  const [uploadProgress, setUploadProgress] = useState(0);
  const [uploadStage, setUploadStage] = useState<"idle" | "uploading">("idle");
  const [uploadDisplayText, setUploadDisplayText] = useState("");
  const [caseOptions, setCaseOptions] = useState<CaseSummary[]>([]);
  const [selectedCaseId, setSelectedCaseId] = useState("");
  const [caseMenuOpen, setCaseMenuOpen] = useState(false);

  const [transform, setTransform] = useState({ x: 240, y: 110, scale: 0.9 });
  const [nodePositions, setNodePositions] = useState<Record<string, Point>>({});

  const [island, setIsland] = useState({ visible: false, x: 20, y: 20, text: "" });

  const wrapperRef = useRef<HTMLDivElement | null>(null);
  const caseSelectorRef = useRef<HTMLDivElement | null>(null);
  const hiddenFileInputRef = useRef<HTMLInputElement | null>(null);
  const uploadAbortRef = useRef<AbortController | null>(null);
  const progressTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const workspaceSyncTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const selectionSyncTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const panStateRef = useRef<{ active: boolean; startMouseX: number; startMouseY: number; startX: number; startY: number }>({
    active: false,
    startMouseX: 0,
    startMouseY: 0,
    startX: 0,
    startY: 0,
  });
  const nodeDragRef = useRef<{ id: string; startMouseX: number; startMouseY: number; startX: number; startY: number } | null>(null);
  const islandDragRef = useRef<{ offsetX: number; offsetY: number } | null>(null);
  const transformRef = useRef(transform);

  useEffect(() => {
    transformRef.current = transform;
  }, [transform]);

  function pickSelectedNodeIds(nextCase: CaseData | null) {
    if (!nextCase) return [] as string[];
    const ids = nextCase.workspace_state?.selected_card_ids ?? [];
    if (ids.length > 0) return ids;
    const focused = nextCase.workspace_state?.focused_card_id;
    return focused ? [focused] : [];
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
          const viewport = current.case.workspace_state?.viewport;
          if (viewport) {
            setTransform({ x: viewport.offset_x, y: viewport.offset_y, scale: viewport.zoom });
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
      nodes.push({
        id: card.id,
        label: card.title,
        meta: card.summary || card.meta_type,
        kind: kindFromMetaType(card.meta_type),
        source: "meta",
        level: card.display_level,
        x: card.position?.x ?? (120 + (index % 4) * 280),
        y: card.position?.y ?? (120 + Math.floor(index / 4) * 170),
      });
    });

    currentCase.inference_cards.slice(0, 16).forEach((card, index) => {
      nodes.push({
        id: card.id,
        label: card.title,
        meta: card.detail.claim,
        kind: "claim",
        source: "inference",
        level: 2,
        x: card.position?.x ?? (1320 + (index % 2) * 300),
        y: card.position?.y ?? (160 + Math.floor(index / 2) * 180),
      });
    });

    evidences.slice(0, 12).forEach((item, index) => {
      nodes.push({
        id: `queued-${item.id}`,
        label: item.file.name,
        meta: item.type,
        kind: "evidence",
        source: "evidence",
        level: 1,
        x: 120 + (index % 3) * 280,
        y: 1180 + Math.floor(index / 3) * 160,
      });
    });

    currentCase.edges.slice(0, 60).forEach((edge) => {
      edges.push({
        id: edge.id,
        sourceId: edge.source,
        targetId: edge.target,
        label: edge.label || edge.edge_type,
        strength: edgeStrength(edge.edge_type),
      });
    });

    return { nodes, edges };
  }, [currentCase, evidences]);

  useEffect(() => {
    if (canvasData.nodes.length === 0) {
      setNodePositions({});
      return;
    }

    setNodePositions((previous) => {
      const next: Record<string, Point> = { ...previous };
      canvasData.nodes.forEach((node) => {
        if (!next[node.id]) {
          next[node.id] = { x: node.x, y: node.y };
        }
      });
      Object.keys(next).forEach((id) => {
        if (!canvasData.nodes.some((node) => node.id === id)) {
          delete next[id];
        }
      });
      return next;
    });
  }, [canvasData.nodes]);

  useEffect(() => {
    const handleMove = (event: MouseEvent) => {
      const draggingNode = nodeDragRef.current;
      if (draggingNode) {
        const scale = transformRef.current.scale;
        const dx = (event.clientX - draggingNode.startMouseX) / scale;
        const dy = (event.clientY - draggingNode.startMouseY) / scale;
        setNodePositions((previous) => ({
          ...previous,
          [draggingNode.id]: { x: draggingNode.startX + dx, y: draggingNode.startY + dy },
        }));
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
        const clampedY = Math.max(ISLAND_PADDING, Math.min(rawY, rect.height - ISLAND_HEIGHT - ISLAND_PADDING));
        setIsland((current) => ({ ...current, x: clampedX, y: clampedY }));
        return;
      }

      const pan = panStateRef.current;
      if (!pan.active) return;
      const dx = event.clientX - pan.startMouseX;
      const dy = event.clientY - pan.startMouseY;
      setTransform((current) => ({ ...current, x: pan.startX + dx, y: pan.startY + dy }));
    };

    const handleUp = () => {
      const draggedNode = nodeDragRef.current;
      const wasPanning = panStateRef.current.active;

      if (islandDragRef.current) {
        const wrapper = wrapperRef.current;
        if (wrapper) {
          const rect = wrapper.getBoundingClientRect();
          setIsland((current) => {
            if (!current.visible) return current;
            const bottomGap = rect.height - (current.y + ISLAND_HEIGHT);
            if (bottomGap <= ISLAND_BOTTOM_DOCK_THRESHOLD) {
              const dockX = Math.max(ISLAND_PADDING, (rect.width - ISLAND_WIDTH) / 2);
              const dockY = Math.max(ISLAND_PADDING, rect.height - ISLAND_HEIGHT - ISLAND_PADDING);
              return { ...current, x: dockX, y: dockY };
            }
            return current;
          });
        }
      }

      islandDragRef.current = null;
      panStateRef.current.active = false;
      nodeDragRef.current = null;

      if (draggedNode) {
        const point = nodePositions[draggedNode.id];
        if (point) {
          void persistCardPosition(draggedNode.id, point);
        }
      }

      if (wasPanning) {
        void persistWorkspace(transformRef.current);
      }
    };

    window.addEventListener("mousemove", handleMove);
    window.addEventListener("mouseup", handleUp);
    return () => {
      window.removeEventListener("mousemove", handleMove);
      window.removeEventListener("mouseup", handleUp);
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
    fireTrack("upload_files_added", [], { count: list.length, file_names: list.map((f) => f.name) });
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
    fireTrack("queued_file_remove", [id]);
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
      await runRelation({ case_id: response.case.case_id });
      const refreshedCase = await getCase();
      setUploadProgress(100);
      setCurrentCase(refreshedCase.case);
      setSelectedNodeIds(pickSelectedNodeIds(refreshedCase.case));
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

  function onCancelUpload() {
    fireTrack("upload_cancel");
    uploadAbortRef.current?.abort();
    stopProgressTimer();
    setCaseLoading(false);
    setUploadProgress(0);
    setUploadStage("idle");
    setUploadDisplayText("");
    setEvidences((current) => current.map((item) => ({ ...item, status: "pending" as const })));
  }

  async function onClearCase() {
    fireTrack("case_clear_requested");
    uploadAbortRef.current?.abort();
    stopProgressTimer();
    setUploadProgress(0);
    setError("");
    await deleteCase();
    setCurrentCase(null);
    setSelectedNodeIds([]);
    setEvidences([]);
    setNodePositions({});
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

  async function persistWorkspace(nextTransform: { x: number; y: number; scale: number }) {
    if (!currentCase?.case_id) return;
    try {
      const updated = await updateWorkspaceState(currentCase.case_id, {
        viewport: {
          zoom: nextTransform.scale,
          offset_x: nextTransform.x,
          offset_y: nextTransform.y,
        },
      });
      setCurrentCase(updated.case);
    } catch {
      // keep local interaction smooth even if persistence fails
    }
  }

  function scheduleWorkspaceSync(nextTransform: { x: number; y: number; scale: number }) {
    if (workspaceSyncTimerRef.current) {
      clearTimeout(workspaceSyncTimerRef.current);
    }
    workspaceSyncTimerRef.current = setTimeout(() => {
      fireTrack("canvas_viewport_update", [], { zoom: nextTransform.scale, offset_x: nextTransform.x, offset_y: nextTransform.y });
      void persistWorkspace(nextTransform);
    }, 260);
  }

  async function persistSelectedNodes(nodeIds: string[], focusedNodeId: string | null) {
    if (!currentCase?.case_id) return;
    const knownCardIds = new Set([
      ...currentCase.meta_cards.map((card) => card.id),
      ...currentCase.inference_cards.map((card) => card.id),
    ]);
    const filteredIds = nodeIds.filter((id) => knownCardIds.has(id));
    const focused = focusedNodeId && knownCardIds.has(focusedNodeId) ? focusedNodeId : (filteredIds[0] ?? null);

    try {
      const updated = await updateWorkspaceState(currentCase.case_id, {
        selected_card_ids: filteredIds,
        focused_card_id: focused,
      });
      setCurrentCase(updated.case);
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
      currentCase.inference_cards.some((card) => card.id === cardId);
    if (!isKnownCard) return;

    try {
      const updated = await updateCardPosition(currentCase.case_id, cardId, point);
      setCurrentCase(updated.case);
    } catch {
      // keep local interaction smooth even if persistence fails
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
    fireTrack("canvas_context_menu");
    const wrapper = wrapperRef.current;
    if (!wrapper) return;
    const rect = wrapper.getBoundingClientRect();

    const rawX = event.clientX - rect.left + ISLAND_PADDING;
    const rawY = event.clientY - rect.top + ISLAND_PADDING;
    const clampedX = Math.max(ISLAND_PADDING, Math.min(rawX, rect.width - ISLAND_WIDTH - ISLAND_PADDING));
    const clampedY = Math.max(ISLAND_PADDING, Math.min(rawY, rect.height - ISLAND_HEIGHT - ISLAND_PADDING));

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
    nodeDragRef.current = {
      id: nodeId,
      startMouseX: event.clientX,
      startMouseY: event.clientY,
      startX: point.x,
      startY: point.y,
    };
  }

  async function onGenerateInferenceFromIsland() {
    fireTrack("island_generate_click", currentCase?.workspace_state?.selected_card_ids ?? []);
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
      const selectedCardIds = currentCase.meta_cards.slice(0, 6).map((card) => card.id);
      await generateInference(currentCase.case_id, prompt, "hypothesis", selectedCardIds);
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
  function onCanvasWheel(event: React.WheelEvent<HTMLDivElement>) {
    event.preventDefault();
    const wrapper = wrapperRef.current;
    if (!wrapper) return;

    const rect = wrapper.getBoundingClientRect();
    const mouseX = event.clientX - rect.left;
    const mouseY = event.clientY - rect.top;

    const delta = -event.deltaY * 0.0012;
    const nextScale = Math.max(0.25, Math.min(2.5, transform.scale * Math.exp(delta)));

    const nextTransform = {
      x: mouseX - (mouseX - transform.x) * (nextScale / transform.scale),
      y: mouseY - (mouseY - transform.y) * (nextScale / transform.scale),
      scale: nextScale,
    };

    setTransform(nextTransform);
    scheduleWorkspaceSync(nextTransform);
  }

  const selectedCaseLabel = caseOptions.find((item) => item.case_id === selectedCaseId)?.title ?? currentCase?.case_title ?? "Case";
  const dropzoneClass = `upload-dropzone figma-dropzone ${isDropActive || isDropSelected ? "active" : ""} ${uploadStage !== "idle" ? "uploading" : ""}`;
  const viewLevel: 1 | 2 | 3 = transform.scale < 1 ? 1 : transform.scale < 1.6 ? 2 : 3;
  const zoomPercent = Math.round(transform.scale * 100);

  function fireTrack(action: string, targets: string[] = [], params: Record<string, unknown> = {}) {
    if (!currentCase?.case_id) return;
    void trackInteraction({
      case_id: currentCase.case_id,
      action,
      targets,
      params,
      context: { selected_case_id: selectedCaseId },
    }).catch(() => {
      // non-blocking telemetry
    });
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
                onClick={() => { fireTrack("case_menu_toggle"); setCaseMenuOpen((current) => !current); }}
                onDoubleClick={() => {
                  fireTrack("case_rename_open");
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
                                setSelectedCaseId(item.case_id);
                                fireTrack("case_switch", [item.case_id]);
                                setEvidences([]);
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
                <article className={`figma-file-card ${selectedFileId === item.id ? "is-selected" : ""}`} key={item.id} onClick={() => { setSelectedFileId(item.id); fireTrack("file_select", [item.id]); }}>
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
    fireTrack("upload_picker_open");
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
            onWheel={onCanvasWheel}
            onContextMenu={onCanvasContextMenu}
            style={{ backgroundPosition: `${transform.x}px ${transform.y}px` }}
          >
            <div
              className="canvas-content"
              style={{
                width: `${CANVAS_WIDTH}px`,
                height: `${CANVAS_HEIGHT}px`,
                transform: `translate(${transform.x}px, ${transform.y}px) scale(${transform.scale})`,
              }}
            >
              <svg className="canvas-links" width={CANVAS_WIDTH} height={CANVAS_HEIGHT}>
                {canvasData.edges.map((edge) => {
                  const source = nodePositions[edge.sourceId];
                  const target = nodePositions[edge.targetId];
                  if (!source || !target) return null;

                  const sx = source.x + NODE_WIDTH / 2;
                  const sy = source.y + NODE_HEIGHT / 2;
                  const tx = target.x + NODE_WIDTH / 2;
                  const ty = target.y + NODE_HEIGHT / 2;
                  const cx = (sx + tx) / 2 + (ty - sy) * 0.15;
                  const cy = (sy + ty) / 2 + (sx - tx) * 0.08;

                  return (
                    <g key={edge.id}>
                      <path d={`M ${sx} ${sy} Q ${cx} ${cy} ${tx} ${ty}`} className="canvas-link" strokeWidth={edge.strength} />
                      <circle cx={(sx + tx) / 2} cy={(sy + ty) / 2} r={7} className="link-hub" />
                      <title>{edge.label}</title>
                    </g>
                  );
                })}
              </svg>

              {canvasData.nodes.map((node) => {
                const point = nodePositions[node.id] ?? { x: node.x, y: node.y };
                const isMeta = node.source === "meta";
                const isInference = node.source === "inference";
                const isEvidence = node.source === "evidence";
                const visualLevel = isEvidence ? 1 : viewLevel;
                const levelClass = isMeta
                  ? `meta-level-${visualLevel}`
                  : isInference
                    ? `infer-level-${visualLevel}`
                    : "";
                const nodeClass = [
                  "canvas-node",
                  `node-${node.kind}`,
                  levelClass,
                  selectedNodeIds.includes(node.id) ? "node-selected" : "",
                ]
                  .filter(Boolean)
                  .join(" ");

                return (
                  <div
                    key={node.id}
                    className={nodeClass}
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

                      const focusedNodeId = nextSelectedNodeIds.includes(node.id)
                        ? node.id
                        : (nextSelectedNodeIds[nextSelectedNodeIds.length - 1] ?? null);

                      fireTrack("node_select", nextSelectedNodeIds, { multi_select: isMultiSelect });
                      scheduleSelectionSync(nextSelectedNodeIds, focusedNodeId);
                      onNodeMouseDown(event, node.id);
                    }}
                  >
                    <div className="node-header">
                      <div className="node-type-icon-wrap">
                        <img className="node-type-icon" src={isMeta ? metaTypeDefaultIcon : isInference ? inferTypeDefaultIcon : detailsIcon} alt={isMeta ? "meta" : isInference ? "inference" : "evidence"} />
                      </div>
                      <span className="node-kind">{isMeta ? "元信息" : isInference ? "推论" : "证据"}</span>
                      <span className="node-id">{node.id}</span>
                    </div>
                    <strong className="node-title" title={node.label}>
                      {node.label}
                    </strong>
                    <p className="node-meta">{firstLine(node.meta) || "-"}</p>
                    {visualLevel === 3 ? <div className="node-preview" /> : null}
                  </div>
                );
              })}
            </div>

            {island.visible ? (
              <div
                className="dynamic-island"
                style={{ left: island.x, top: island.y }}
                onMouseDown={onIslandMouseDown}
                onContextMenu={(event) => event.preventDefault()}
              >
                <img src={writeIcon} alt="write" className="island-icon" />
                <input
                  value={island.text}
                  onChange={(event) => setIsland((current) => ({ ...current, text: event.target.value }))}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") {
                      event.preventDefault();
                      void onGenerateInferenceFromIsland();
                    }
                  }}
                  placeholder="Generate inference..."
                />
                <div className="island-spacer" />
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
            <div className="canvas-zoom-badge">{zoomPercent}%</div>
          </div>
        </section>
      </div>
    </main>
  );
}










































































































