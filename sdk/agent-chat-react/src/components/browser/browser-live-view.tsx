interface BrowserLiveViewProps {
  src: string;
  testId?: string;
}

export function BrowserLiveView({
  src,
  testId = "browser-iframe",
}: BrowserLiveViewProps) {
  return (
    <iframe
      data-testid={testId}
      title="Browser live view"
      src={src}
      className="h-full w-full border-0 bg-black"
      allow="clipboard-read; clipboard-write"
    />
  );
}
