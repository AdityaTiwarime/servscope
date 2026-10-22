// Small stroke icons, drawn in currentColor. Decorative unless given a label.

type Props = { size?: number; label?: string };

function Svg({ size = 16, label, children }: Props & { children: React.ReactNode }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.5}
      strokeLinecap="round"
      strokeLinejoin="round"
      {...(label ? { role: "img", "aria-label": label } : { "aria-hidden": true })}
    >
      {children}
    </svg>
  );
}

export const Logo = () => (
  <svg width={26} height={26} viewBox="0 0 26 26" aria-hidden="true">
    <circle cx="13" cy="13" r="11" fill="none" stroke="var(--accent-strong)" strokeWidth="2" />
    <circle cx="13" cy="13" r="5" fill="none" stroke="var(--accent-strong)" strokeWidth="2" />
    <path d="M13 2v6M24 13h-6" stroke="var(--accent-strong)" strokeWidth="2" strokeLinecap="round" />
  </svg>
);

export const OverviewIcon = (p: Props) => (
  <Svg {...p}><rect x="1.5" y="1.5" width="5" height="5" rx="1" /><rect x="9.5" y="1.5" width="5" height="5" rx="1" /><rect x="1.5" y="9.5" width="5" height="5" rx="1" /><rect x="9.5" y="9.5" width="5" height="5" rx="1" /></Svg>
);
export const ServicesIcon = (p: Props) => (
  <Svg {...p}><circle cx="3.5" cy="8" r="2" /><circle cx="12.5" cy="3.5" r="2" /><circle cx="12.5" cy="12.5" r="2" /><path d="M5.5 7l5-2.6M5.5 9l5 2.6" /></Svg>
);
export const TracesIcon = (p: Props) => <Svg {...p}><path d="M2 3.5h8M4 8h9M6 12.5h6" /></Svg>;
export const LogsIcon = (p: Props) => <Svg {...p}><path d="M2.5 3h11M2.5 6.3h11M2.5 9.6h7M2.5 13h9" /></Svg>;
export const BellIcon = (p: Props) => (
  <Svg {...p}><path d="M8 2a4 4 0 0 0-4 4v3l-1.5 2.5h11L12 9V6a4 4 0 0 0-4-4zM6.5 13.5a1.5 1.5 0 0 0 3 0" /></Svg>
);
export const PlayIcon = (p: Props) => <Svg {...p}><path d="M3.5 2.5l9.5 5.5-9.5 5.5z" /></Svg>;
export const GearIcon = (p: Props) => (
  <Svg {...p}><circle cx="8" cy="8" r="2.3" /><path d="M8 1.5v2M8 12.5v2M1.5 8h2M12.5 8h2M3.4 3.4l1.4 1.4M11.2 11.2l1.4 1.4M3.4 12.6l1.4-1.4M11.2 4.8l1.4-1.4" /></Svg>
);
export const SearchIcon = (p: Props) => <Svg {...p}><circle cx="7" cy="7" r="5" /><path d="M11 11l3.5 3.5" /></Svg>;
export const AlertIcon = (p: Props) => (
  <Svg {...p}><path d="M8 1.8l6.5 11.4h-13z" /><path d="M8 6.3v3.2" /><circle cx="8" cy="11.4" r=".6" fill="currentColor" /></Svg>
);
export const CheckIcon = (p: Props) => <Svg {...p}><circle cx="8" cy="8" r="6.5" /><path d="M5.3 8.2l1.9 1.9 3.5-3.8" /></Svg>;
export const ErrorIcon = (p: Props) => (
  <Svg {...p}><circle cx="8" cy="8" r="6.5" /><path d="M8 4.8v3.6" /><circle cx="8" cy="11" r=".6" fill="currentColor" /></Svg>
);
export const ArrowRight = (p: Props) => <Svg {...p}><path d="M3 8h9M8.5 4l4 4-4 4" /></Svg>;
export const ArrowLeft = (p: Props) => <Svg {...p}><path d="M10 3.5L5.5 8l4.5 4.5" /></Svg>;
export const CopyIcon = (p: Props) => (
  <Svg {...p}><rect x="5" y="5" width="8.5" height="8.5" rx="1.5" /><path d="M3 10.5V3.8c0-.4.4-.8.8-.8h6.7" /></Svg>
);
export const SunIcon = (p: Props) => (
  <Svg {...p}><circle cx="8" cy="8" r="3" /><path d="M8 1v1.5M8 13.5V15M1 8h1.5M13.5 8H15M3 3l1 1M12 12l1 1M3 13l1-1M12 4l1-1" /></Svg>
);
export const MoonIcon = (p: Props) => <Svg {...p}><path d="M13 9.5A5.5 5.5 0 0 1 6.5 3a5.5 5.5 0 1 0 6.5 6.5z" /></Svg>;
export const RetryIcon = (p: Props) => <Svg {...p}><path d="M13 8a5 5 0 1 1-1.5-3.6M13 2.5v3h-3" /></Svg>;
