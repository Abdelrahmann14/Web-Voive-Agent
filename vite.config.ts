import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
import basicSsl from '@vitejs/plugin-basic-ssl';
import path from 'path';
import {defineConfig, loadEnv} from 'vite';

export default defineConfig(({mode}) => {
  const env = loadEnv(mode, '.', '');
  return {
    // basicSsl serves the dev site over HTTPS so the microphone (getUserMedia)
    // works from any address — LAN IPs and other devices, not just localhost.
    plugins: [react(), tailwindcss(), basicSsl()],
    define: {
      'process.env.GEMINI_API_KEY': JSON.stringify(env.GEMINI_API_KEY),
    },
    resolve: {
      alias: {
        '@': path.resolve(__dirname, '.'),
      },
    },
    server: {
      // Proxy token/health requests to the FastAPI server (backend/server.py).
      proxy: {
        '/token': 'http://localhost:8000',
        '/health': 'http://localhost:8000',
        '/summarize': 'http://localhost:8000',
        '/export': 'http://localhost:8000',
      },
      // HMR is disabled in AI Studio via DISABLE_HMR env var.
      // Do not modifyâfile watching is disabled to prevent flickering during agent edits.
      hmr: process.env.DISABLE_HMR !== 'true',
      // Disable file watching when DISABLE_HMR is true to save CPU during agent edits.
      watch: process.env.DISABLE_HMR === 'true' ? null : {},
    },
  };
});
