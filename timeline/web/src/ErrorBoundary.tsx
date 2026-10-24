import { Component, type ReactNode } from "react";

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
}

/** Keeps a failed chunk load or render error from blanking the whole app. */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="card empty" role="alert">
        <p>
          <strong>Something went wrong while showing this page.</strong>
        </p>
        <p className="muted">{this.state.error.message}</p>
        <p>
          <a href="#/">Back to all incidents</a>
        </p>
      </div>
    );
  }
}
