// Vendored from border-beam v1.3.0 (https://github.com/Jakubantalik/border-beam)
// MIT License, Copyright (c) Jakub Antalik — see LICENSE-border-beam.md
// Local modifications: 'brand' amber color variant; theme auto-detection
// extended to honor an ancestor .dark/.light class or data-theme attribute.

export { BorderBeam } from './BorderBeam';
export { default } from './BorderBeam';

export type {
  BorderBeamProps,
  BorderBeamSize,
  BorderBeamTheme,
  BorderBeamColorVariant,
  SizeConfig,
  ThemeColors,
} from './types';

export { sizePresets, sizeThemePresets, themeColors } from './styles';
