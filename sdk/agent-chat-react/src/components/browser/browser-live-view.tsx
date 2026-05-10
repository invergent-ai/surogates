export function BrowserLiveView({ src }: { src: string }) {
  return (
    <iframe
      data-testid="browser-iframe"
      title="Browser live view"
      src={src}
      className="h-full w-full border-0 bg-black"
      allow="clipboard-read; clipboard-write"
    />
  );
}
