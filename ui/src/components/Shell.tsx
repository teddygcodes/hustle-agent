import { Outlet } from 'react-router-dom';
import { Sidebar } from './Sidebar';
import { MobileNav } from './MobileNav';

export function Shell() {
  return (
    <div className="flex min-h-screen" style={{ background: 'var(--nest-bg)' }}>
      <Sidebar />
      <MobileNav />
      <main className="flex-1 min-w-0 md:ml-60 p-6 max-md:pb-20">
        <Outlet />
      </main>
    </div>
  );
}
