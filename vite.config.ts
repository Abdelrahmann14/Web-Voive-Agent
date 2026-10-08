import basicSsl from '@vitejs/plugin-basic-ssl';
import {defineConfig} from 'vite';

export default defineConfig(({mode}) => ({
  // basicSsl serves the dev site over HTTPS so the microphone (getUserMedia)
  // works from any address, LAN IPs and other devices, not just localhost.
  // `vite --mode http` skips it: plain http://localhost is already a secure
  // context, and some browsers refuse the self-signed certificate.
  plugins: mode === 'http' ? [] : [basicSsl()],
  server: {
    // Proxy the API calls to the FastAPI server (backend/server.py).
    proxy: {
      '/token': 'http://localhost:8000',
      '/health': 'http://localhost:8000',
      '/summarize': 'http://localhost:8000',
      '/export': 'http://localhost:8000',
    },
  },
}));
