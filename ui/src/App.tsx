import { BrowserRouter, Routes, Route } from 'react-router-dom';
import { Shell } from './components/Shell';
import CommandCenter from './pages/CommandCenter';
import Finances from './pages/Finances';
import Strategies from './pages/Strategies';
import Projections from './pages/Projections';
import Pipeline from './pages/Pipeline';
import Dream from './pages/Dream';
import Journal from './pages/Journal';
import Chat from './pages/Chat';
import Proposals from './pages/Proposals';
import Health from './pages/Health';
import Instincts from './pages/Instincts';

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<Shell />}>
          <Route index element={<CommandCenter />} />
          <Route path="finances" element={<Finances />} />
          <Route path="strategies" element={<Strategies />} />
          <Route path="projections" element={<Projections />} />
          <Route path="pipeline" element={<Pipeline />} />
          <Route path="dream" element={<Dream />} />
          <Route path="journal" element={<Journal />} />
          <Route path="chat" element={<Chat />} />
          <Route path="proposals" element={<Proposals />} />
          <Route path="health" element={<Health />} />
          <Route path="instincts" element={<Instincts />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}
