import React from 'react'
import { HashRouter as Router, Routes, Route, NavLink, Navigate } from 'react-router-dom'
import Dashboard from './pages/Dashboard.jsx'
import Contacts from './pages/Contacts.jsx'
import ContactDetail from './pages/ContactDetail.jsx'
import Deals from './pages/Deals.jsx'
import DealDetail from './pages/DealDetail.jsx'
import Analytics from './pages/Analytics.jsx'
import Goals from './pages/Goals.jsx'
import Activity from './pages/Activity.jsx'
import Approvals from './pages/Approvals.jsx'
import Marketing from './pages/Marketing.jsx'
import Customers from './pages/Customers.jsx'
import Automation from './pages/Automation.jsx'
import Agents from './pages/Agents.jsx'
import KnowledgeBase from './pages/KnowledgeBase.jsx'
import Integrations from './pages/Integrations.jsx'
import Copilot from './pages/Copilot.jsx'
import Login from './pages/Login.jsx'
import { isAuthed, logout } from './api.js'

const NAV_LINKS = [
  { to: '/', label: 'Dashboard', end: true },
  { to: '/copilot', label: 'Copilot' },
  { to: '/contacts', label: 'Contacts' },
  { to: '/deals', label: 'Deals' },
  { to: '/customers', label: 'Customers' },
  { to: '/marketing', label: 'Marketing' },
  { to: '/analytics', label: 'Analytics' },
  { to: '/goals', label: 'Goals' },
  { to: '/approvals', label: 'Approvals' },
  { to: '/automation', label: 'Automation' },
  { to: '/agents', label: 'Agents' },
  { to: '/knowledge-base', label: 'Knowledge Base' },
  { to: '/integrations', label: 'Integrations' },
  { to: '/activity', label: 'Activity' },
]

function RequireAuth({ children }) {
  if (!isAuthed()) return <Navigate to="/login" replace />
  return children
}

export default function App() {
  return (
    <Router>
      <div className="App">
        <nav className="navbar">
          <div className="nav-container">
            <NavLink to="/" className="nav-logo">
              Founder OS
            </NavLink>
            {isAuthed() && (
              <ul className="nav-menu">
                {NAV_LINKS.map(link => (
                  <li key={link.to}>
                    <NavLink to={link.to} className="nav-link" end={link.end}>
                      {link.label}
                    </NavLink>
                  </li>
                ))}
                <li>
                  <button
                    onClick={logout}
                    className="nav-link"
                    style={{ background: 'none', border: 'none', cursor: 'pointer', font: 'inherit' }}
                  >
                    Logout
                  </button>
                </li>
              </ul>
            )}
          </div>
        </nav>

        <div className="main-content">
          <Routes>
            <Route path="/login" element={<Login />} />
            <Route path="/" element={<RequireAuth><Dashboard /></RequireAuth>} />
            <Route path="/copilot" element={<RequireAuth><Copilot /></RequireAuth>} />
            <Route path="/contacts" element={<RequireAuth><Contacts /></RequireAuth>} />
            <Route path="/contacts/:id" element={<RequireAuth><ContactDetail /></RequireAuth>} />
            <Route path="/deals" element={<RequireAuth><Deals /></RequireAuth>} />
            <Route path="/deals/:id" element={<RequireAuth><DealDetail /></RequireAuth>} />
            <Route path="/customers" element={<RequireAuth><Customers /></RequireAuth>} />
            <Route path="/marketing" element={<RequireAuth><Marketing /></RequireAuth>} />
            <Route path="/analytics" element={<RequireAuth><Analytics /></RequireAuth>} />
            <Route path="/goals" element={<RequireAuth><Goals /></RequireAuth>} />
            <Route path="/approvals" element={<RequireAuth><Approvals /></RequireAuth>} />
            <Route path="/automation" element={<RequireAuth><Automation /></RequireAuth>} />
            <Route path="/agents" element={<RequireAuth><Agents /></RequireAuth>} />
            <Route path="/knowledge-base" element={<RequireAuth><KnowledgeBase /></RequireAuth>} />
            <Route path="/integrations" element={<RequireAuth><Integrations /></RequireAuth>} />
            <Route path="/activity" element={<RequireAuth><Activity /></RequireAuth>} />
          </Routes>
        </div>
      </div>
    </Router>
  )
}
