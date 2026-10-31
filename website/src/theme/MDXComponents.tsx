/**
 * Global MDX component registry. Curriculum lessons (curriculum/**\/docs/en.md)
 * use these tags without importing them, so the markdown stays plain on GitHub.
 */
import MDXComponents from '@theme-original/MDXComponents';
import Quiz from '@site/src/components/Curriculum/Quiz';
import LessonMeta from '@site/src/components/Curriculum/LessonMeta';
import ShipIt from '@site/src/components/Curriculum/ShipIt';
import RunTests from '@site/src/components/Curriculum/RunTests';
import CurriculumHome from '@site/src/components/Curriculum/CurriculumHome';
import LearningPath from '@site/src/components/Curriculum/LearningPath';

export default {
  ...MDXComponents,
  Quiz,
  LessonMeta,
  ShipIt,
  RunTests,
  CurriculumHome,
  LearningPath,
};
