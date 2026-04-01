import { Outlet } from 'react-router-dom';
import { Sidebar } from './Sidebar';
import { MobileNav } from './MobileNav';

export function Shell() {
  return (
    <div className="flex min-h-screen bg-zinc-950">
      <Sidebar />
      <MobileNav />
      <main className="flex-1 md:ml-60 p-6 max-md:pb-20">
        <Outlet />
      </main>
    </div>
  );
}
