export function App() {
  return (
    <main className="shell">
      <div className="ambient ambient-left" aria-hidden="true" />
      <div className="ambient ambient-right" aria-hidden="true" />

      <header className="topbar">
        <a className="brand" href="/" aria-label="FlowSight home">
          <span className="brand-mark" aria-hidden="true">
            <span />
            <span />
            <span />
          </span>
          <span>FlowSight</span>
        </a>
        <span className="environment">Local development</span>
      </header>

      <section className="empty-state" aria-labelledby="empty-state-title">
        <div className="map-preview" aria-hidden="true">
          <span className="map-line map-line-one" />
          <span className="map-line map-line-two" />
          <span className="map-node map-node-one" />
          <span className="map-node map-node-two" />
          <span className="map-node map-node-three" />
        </div>

        <p className="eyebrow">Runtime visualization map</p>
        <h1 id="empty-state-title">Your FlowSight shell is ready.</h1>
        <p className="summary">
          The bundled interface is installed and served by the local sidecar.
        </p>
        <div className="status" role="status">
          <span className="status-dot" aria-hidden="true" />
          <span>No runtime data in this Phase 0 shell</span>
        </div>
      </section>

      <footer>
        <span>Local-only</span>
        <span aria-hidden="true">·</span>
        <span>Read-only UI bundle</span>
      </footer>
    </main>
  );
}
