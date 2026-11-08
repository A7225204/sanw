export type ScopeId = string;

export interface StarfieldPoint {
  x: number;
  y: number;
  z: number;
  size: number;
  brightness: number;
  color: string;
}

export interface MemoryNode {
  id: string;
  scope: ScopeId;
  parentId: string | null;
  topicId: string | null;
  summary: string;
  sourceMessageIds: string[];
  retentionScore: number;
  importance: number;
  recency: number;
  frequency: number;
  sentiment?: number; // -1 负面 ~ 1 正面
  emotion?: string;   // 怒/累/丧/烦/害怕/正面/中性
  trauma?: { charge: number; triggers: string[]; since: string };
  createdAt: string;
  lastAccessedAt: string;
  kind?: 'scope' | 'topic' | 'memory' | 'persona' | 'category';
  category?: string;  // 人格视图里的分类：long/card/growth/short/exp/center
  t?: number;         // 毫秒时间戳，历史进度条用
  uid?: string;       // 人物卡节点对应的 QQ
  status?: string;    // 人格成长球的状态：pending/solidified/dismissed
  starfield?: StarfieldPoint;
}

export interface TreeEdge {
  source: string;
  target: string;
  kind: 'hierarchy';
}

export interface StarfieldEdge {
  source: string;
  target: string;
  weight: number;
  kind?: 'intra' | 'cross' | 'owner';
}

export interface MemoryTreeResponse {
  scope: ScopeId;
  nodes: MemoryNode[];
  edges: TreeEdge[];
  generatedAt: string;
}

export interface MemoryStarfieldResponse {
  scope: ScopeId;
  nodes: MemoryNode[];
  edges: StarfieldEdge[];
  generatedAt: string;
  method?: string;
}

export interface SpeakerInfo {
  channel: 'group' | 'private' | 'other';
  scopeLabel: string;
  groupId: string;
  speaker: string;
  uid: string;
}

export interface PersonElsewhere {
  id: string;
  scope: string;
  scopeLabel: string;
  summary: string;
  viaQq: boolean;
}

export interface TraumaStatus {
  charge: number;
  progress: number;
  remainingSec: number;
  repairing: boolean;
  triggers: string[];
  since: string;
}

export interface MemoryDetailResponse {
  /** 2026-11-07：念头池条目（thought:<id>）的专属字段，面板 ThoughtPanel 用 */
  thought?: {
    id?: number; text?: string; why?: string; priority?: number;
    poked?: number; created?: number; src?: string; scope?: string; ttl?: number;
  };
  node: MemoryNode;
  speaker?: SpeakerInfo;
  personElsewhere?: PersonElsewhere[];
  trauma?: TraumaStatus | null;
  dream?: {
    dream: string; kind: string; at: string;
    hormones?: Record<string, number>;
    insights?: string[];
  } | null;
  /** 三层人格：本我 / 自我 / 超我 */
  triad?: {
    id: { label: string; want: string; values: Record<string, number>; desire: Record<string, number>; words?: string[] };
    ego: { label: string; state: string; blame: number; words?: string[] };
    superego: { label: string; rules: string[]; count: number; words?: string[] };
    conflict?: unknown | null;
    meta?: { last_run?: string; updated_at?: string } | null;
  } | null;
  /** 点 persona:ego / persona:superego / persona:id 时返回的单层详情 */
  triadPart?: {
    label?: string; want?: string; state?: string; blame?: number;
    values?: Record<string, number>; desire?: Record<string, number>;
    words?: string[]; rules?: string[]; count?: number;
  } | null;
  soulItem?: {
    kind: 'trait' | 'rule' | 'defense';
    label: string; text: string; why: string; at: string;
    solidified: boolean; index: number; total: number;
  } | null;
  soul?: {
    traits: string[]; rules: string[]; defense: string[];
    total: number; mood?: Record<string, number>;
    moodKind?: string; lastRun?: string;
    updatedAt?: string;
    traitItems?: { text: string; why: string; at: string; solidified?: boolean }[];
    ruleItems?: { text: string; why: string; at: string; solidified?: boolean }[];
    /** 三我词条（弗洛伊德结构）：面板按这个分组，而不是旧的 traits/rules */
    id?: string[]; ego?: string[]; superego?: string[];
    /** 三我词条：need=本我需求分类（身体/依恋/探索/掌控），sub=超我的底线|向往 */
    idItems?: { text: string; why: string; at: string; solidified?: boolean; need?: string; sub?: string }[];
    egoItems?: { text: string; why: string; at: string; solidified?: boolean; need?: string; sub?: string }[];
    superegoItems?: { text: string; why: string; at: string; solidified?: boolean; need?: string; sub?: string }[];
    /** 自我四项功能评分 0~1（现实检验/冲动控制/延迟满足/情绪调节） */
    egoFunctions?: Record<string, number>;
    /** 超我严厉度 0~1：越高越苛责自己 */
    superegoSeverity?: number;
    /** 三我势力 0~1 */
    forces?: Record<string, number>;
    /** 冲突 7 天统计 */
    conflictStats?: {
      days?: { date: string; count: number }[];
      kinds?: [string, number][];
      total?: number;
    };
    /** 防御机制 + 关联创伤（点一下能跳到 trauma:<i>） */
    defenseItems?: {
      text: string; why: string; at: string; index: number;
      traumaNodeId?: string; traumaSubject?: string;
      charge?: number; triggers?: string[];
    }[];
    /** 人格成长条目（提炼产出，也算词条） */
    growth?: {
      id: string; trait: string; value: string;
      status: string; category?: string; at?: string;
    }[];
    /** 候选池：还没上位的词条（手册 G.26） */
    pools?: { traits: string[]; rules: string[] };
    poolTotal?: number;
    allTotal?: number;
  } | null;
  hormones?: Record<string, number> | null;
  card?: {
    称呼?: string; 性别?: string; 职业?: string; 作息?: string;
    兴趣?: string[]; 语言习惯?: string[]; 性格?: string;
    updatedAt?: string; sourceCount?: number;
  } | null;
  relationMemory?: {
    好感?: number; 恩怨?: string[]; 互动模式?: string; updatedAt?: string;
  } | null;
  sourceMessages: { id: string; text: string; senderId: string; ts: string }[];
  relatedNodeIds: string[];
  related?: { id: string; summary: string; kind: string }[];
  /** 分类节点（pool:trauma / pool:thoughts …）的池信息 */
  pool?: { kind: string; count: number };
  /** 宏观刻度节点（白洞/黑洞）的说明文字 */
  note?: string;
  /** 人格中心的"最近互动" */
  personaTop?: { uid: string; last: number }[];
  growth?: {
    id: string;
    trait: string;
    value: string;
    status: string;
    since: string;
    evidence: string[];
  } | null;
}

export interface ScopeSummary {
  scope: ScopeId;
  label: string;
  count: number;
  topics: number;
}
