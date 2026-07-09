import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import '@xyflow/react/dist/style.css';

import App from './App';
import './styles.css';

const root = document.getElementById('root');

if (!root) throw new Error('FlowSight root element is missing.');

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
