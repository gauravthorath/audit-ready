/**
 * The AuditReady mark, inline so it is one HTTP request fewer and can sit in
 * flex layouts at any size. Source of truth is docs/brand/auditready-mark.svg -
 * keep the two in step.
 *
 * The green is fixed, not a theme token: the mark is the one thing that should
 * look identical in light, dark, on the site and on a slide.
 */

export function Logo({ size = 24, className }: { size?: number; className?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      width={size}
      height={size}
      className={className}
      role="img"
      aria-label="AuditReady"
    >
      <rect x="1" y="1" width="22" height="22" rx="5.5" fill="#0F6E56" />
      <path
        d="M7 12.4l3.3 3.3L17.4 8.6"
        fill="none"
        stroke="#FFFFFF"
        strokeWidth="2.3"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path d="M7.5 18.6h9" fill="none" stroke="#FFFFFF" strokeOpacity=".6" strokeWidth="1.7" strokeLinecap="round" />
    </svg>
  );
}

/** Mark + name, for the sidebar header and empty states. */
export function Wordmark({ size = 24, className = "" }: { size?: number; className?: string }) {
  return (
    <span className={`inline-flex items-center gap-2.5 ${className}`}>
      <Logo size={size} />
      <span className="text-[19px] font-semibold tracking-tight">AuditReady</span>
    </span>
  );
}
