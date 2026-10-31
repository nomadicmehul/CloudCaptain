import React from 'react';
import {curriculumModel} from './curriculumTitles';

/**
 * <ToolVersion name="kubernetes" /> → "v1.37.2" (from curriculum/versions.json,
 * refreshed weekly by versions-weekly.yml). Lets lesson text say "the current
 * release" without going stale.
 */
export default function ToolVersion({name, fallback = 'the current release'}: {name: string; fallback?: string}): React.JSX.Element {
  const entry = curriculumModel.versions?.versions?.[name];
  const label = curriculumModel.versions?.track?.[name]?.label || name;
  if (!entry) return <span title={`${label}: version not tracked`}>{fallback}</span>;
  return (
    <span title={`${label} ${entry.version}${entry.publishedAt ? `, released ${entry.publishedAt}` : ''}; tracked weekly`}>
      {entry.version}
    </span>
  );
}
