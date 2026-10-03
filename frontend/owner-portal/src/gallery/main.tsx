// Development-only component gallery (gallery.html). The production build
// has a single entry, index.html, so none of this ships.
import React from 'react';
import { createRoot } from 'react-dom/client';
import '../styles/app.css';
import './gallery.css';
import { Gallery } from './Gallery';

createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <Gallery />
  </React.StrictMode>,
);
