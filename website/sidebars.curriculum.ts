/**
 * Sidebar for the curriculum docs instance (/learn).
 *
 * Generated: scripts/build-curriculum.mjs writes src/data/generated/curriculum.json
 * before every start/build (see package.json prestart/prebuild). Do not edit by hand;
 * add a lesson folder under curriculum/phases/ instead.
 */
import type {SidebarsConfig} from '@docusaurus/plugin-content-docs';
import curriculum from './src/data/generated/curriculum.json';

const sidebars: SidebarsConfig = {
  curriculumSidebar: curriculum.sidebar as unknown as SidebarsConfig[string],
};

export default sidebars;
