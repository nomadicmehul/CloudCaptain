export type LessonType = 'Build' | 'Learn';
export type QuizStage = 'pre' | 'check' | 'post';

export type LessonMetaData = {
  id: string;
  title: string;
  short?: string | null;
  motto: string;
  type: LessonType;
  status: 'draft' | 'preview' | 'published';
  minutes: number;
  langs: string[];
  prereqs: string[];
  tools?: string[];
  reference?: string[];
  outputs: string[];
  tests: Record<string, string>;
  last_verified?: string;
  phase: string;
  phaseTitle: string;
  phaseNumber: string;
  dir: string;
  repo: string;
  branch: string;
};

export type QuizQuestion = {
  id: string;
  stage: QuizStage;
  prompt: string;
  choices: string[];
  answer: number;
  explanation: string;
};

export type QuizData = {
  lesson: string;
  questions: QuizQuestion[];
};

export type CurriculumLesson = {
  id: string;
  slug: string;
  number: string;
  index: number;
  title: string;
  short: string | null;
  motto: string;
  type: LessonType;
  status: string;
  minutes: number;
  langs: string[];
  prereqs: string[];
  tools: string[];
  reference: string[];
  outputs: string[];
  tests: Record<string, string>;
  lastVerified: string | null;
  phase: string;
  phaseTitle: string;
  phaseNumber: string;
  dir: string;
  docId: string;
  route: string;
  testCount: number;
};

export type CurriculumPhase = {
  number: string;
  dir: string;
  slug: string;
  title: string;
  icon: string;
  tagline: string;
  docId: string;
  route: string;
  live: boolean;
  minutes: number;
  lessons: CurriculumLesson[];
};

export type CurriculumPath = {
  id: string;
  title: string;
  description: string;
  careerPath: string | null;
  route: string;
  docId: string;
  minutes: number;
  lessons: CurriculumLesson[];
};

export type CurriculumModel = {
  generatedAt: string;
  site: {routeBasePath: string; repo: string; branch?: string};
  stats: {
    lessons: number;
    buildLessons: number;
    learnLessons: number;
    minutes: number;
    hours: number;
    tests: number;
    quizzes: number;
    phasesLive: number;
    phasesTotal: number;
  };
  phases: CurriculumPhase[];
  paths: CurriculumPath[];
  certifications: {id: string; title: string; vendor?: string; referenceDoc: string; phases?: string[]}[];
  backlinks: Record<string, string[]>;
  lessonsById: Record<string, CurriculumLesson>;
  versions?: {
    updatedAt: string;
    track: Record<string, {repo: string; label: string}>;
    versions: Record<string, {version: string; publishedAt: string}>;
  } | null;
  deprecations?: Deprecation[];
};

export type Deprecation = {
  id: string;
  term: string;
  pattern?: string;
  status: string;
  date: string;
  replacement: string;
  link?: string;
};
