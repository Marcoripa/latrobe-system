import React, { useEffect, useState } from 'react';
import { collection, query, where, onSnapshot } from 'firebase/firestore';
import { motion } from 'framer-motion';
import {
  MapPin,
  CheckCircle2,
  FolderKanban,
  AlertCircle,
  Plus,
  X,
  User,
} from 'lucide-react';
import { db } from '../../lib/firebase';
import {
  createProjectViaBackend,
  fetchProjectsViaBackend,
  getCachedProjects,
  setCachedProjects,
} from '../../lib/offlineQueue';
import { useAuth } from '../../context/AuthContext';
import { FatButton } from '../../components/ui/FatButton';

/**
 * Step 1: Project Selection
 * Queries active projects from the 'projects' Firestore collection (offline-cached via IndexedDB)
 * with automatic fallback to the FastAPI backend (/api/v1/projects) and localStorage cache.
 * Includes a "New Project" modal requiring 1) Project Name and 2) Client.
 */
export function Step1Project({ selectedProject, onSelectProject }) {
  const { currentUser } = useAuth();
  const [projects, setProjects] = useState(() => getCachedProjects());
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  // New Project Modal State
  const [isModalOpen, setIsModalOpen] = useState(false);
  const [newProjectName, setNewProjectName] = useState('');
  const [newClientName, setNewClientName] = useState('');
  const [creatingProject, setCreatingProject] = useState(false);
  const [modalError, setModalError] = useState('');

  useEffect(() => {
    let isMounted = true;
    setLoading(true);
    setError('');

    const filterForOperator = (rawProjects) =>
      rawProjects.filter((project) => {
        if (
          Array.isArray(project.assignedOperators) &&
          project.assignedOperators.length > 0 &&
          currentUser?.uid
        ) {
          return project.assignedOperators.includes(currentUser.uid);
        }
        if (project.assignedTo && currentUser?.uid) {
          return project.assignedTo === currentUser.uid;
        }
        return true;
      });

    const loadViaBackendOrCache = async () => {
      try {
        if (typeof navigator === 'undefined' || navigator.onLine) {
          const backendProjects = await fetchProjectsViaBackend();
          if (!isMounted) return;
          const filtered = filterForOperator(backendProjects);
          setProjects(filtered);
          setCachedProjects(filtered);
          setError('');
          setLoading(false);
          return;
        }
      } catch (backendErr) {
        console.warn('[Step1Project] Backend fallback error:', backendErr);
      }

      if (!isMounted) return;
      const cached = getCachedProjects();
      if (cached.length > 0) {
        setProjects(filterForOperator(cached));
        setError('');
      } else {
        setError('Could not load projects.');
      }
      setLoading(false);
    };

    const projectsRef = collection(db, 'projects');
    const q = query(projectsRef, where('status', '==', 'active'));

    const unsubscribe = onSnapshot(
      q,
      (snapshot) => {
        if (!isMounted) return;
        const fetched = snapshot.docs.map((doc) => ({
          id: doc.id,
          ...doc.data(),
        }));

        const filtered = filterForOperator(fetched);
        setProjects(filtered);
        setCachedProjects(filtered);
        setError('');
        setLoading(false);
      },
      (err) => {
        console.warn(
          '[Step1Project] Direct Firestore read failed; falling back to backend API / local cache:',
          err.message || err
        );
        loadViaBackendOrCache();
      }
    );

    return () => {
      isMounted = false;
      unsubscribe();
    };
  }, [currentUser?.uid]);

  const handleOpenModal = () => {
    setModalError('');
    setNewProjectName('');
    setNewClientName('');
    setIsModalOpen(true);
  };

  const handleCloseModal = () => {
    if (creatingProject) return;
    setIsModalOpen(false);
    setModalError('');
  };

  const handleCreateProject = async (e) => {
    e.preventDefault();
    setModalError('');

    if (!newProjectName.trim() || !newClientName.trim()) {
      setModalError('Please enter both Project Name and Client.');
      return;
    }

    try {
      setCreatingProject(true);
      const created = await createProjectViaBackend({
        name: newProjectName.trim(),
        client_name: newClientName.trim(),
      });

      setProjects((prev) =>
        prev.some((p) => p.id === created.id) ? prev : [created, ...prev]
      );
      onSelectProject(created);
      setError('');
      setIsModalOpen(false);
    } catch (err) {
      console.error('[Step1Project] Failed to create project:', err);
      setModalError(err.message || 'Failed to create project.');
    } finally {
      setCreatingProject(false);
    }
  };

  return (
    <div className="space-y-5">
      <div className="flex items-end justify-between gap-4">
        <div>
          <span className="text-xs font-black uppercase tracking-widest text-primary">
            Step 1 of 5
          </span>
          <h2 className="text-2xl sm:text-3xl font-black uppercase tracking-tight text-white mt-1">
            Tap Active Project
          </h2>
        </div>

        <button
          type="button"
          onClick={handleOpenModal}
          className="
            min-h-touch px-4 py-3 rounded-xl
            bg-primary text-black border-2 border-white
            inline-flex items-center gap-2 shrink-0
            font-black text-sm sm:text-base uppercase tracking-wider
            shadow-hard-sm hover:opacity-95 active:scale-95 transition
            focus:outline-none focus:ring-4 focus:ring-white
          "
        >
          <Plus className="w-6 h-6 stroke-[3]" aria-hidden="true" />
          <span>New Project</span>
        </button>
      </div>

      {loading && (
        <div className="p-8 rounded-2xl bg-dark-surface border-2 border-dark-border text-center">
          <p className="text-xl font-black uppercase tracking-wider text-primary animate-pulse">
            Loading Projects...
          </p>
        </div>
      )}

      {!loading && error && (
        <div
          role="alert"
          className="p-5 rounded-2xl bg-safety-red text-white border-2 border-white flex items-center gap-3 font-extrabold uppercase"
        >
          <AlertCircle className="w-8 h-8 shrink-0 stroke-[2.75]" aria-hidden="true" />
          <span>{error}</span>
        </div>
      )}

      {!loading && !error && projects.length === 0 && (
        <div className="p-8 rounded-2xl bg-dark-surface border-2 border-dark-border text-center space-y-4">
          <FolderKanban className="w-12 h-12 text-primary mx-auto stroke-[2.5]" aria-hidden="true" />
          <div className="space-y-1">
            <p className="text-xl font-black uppercase text-white">
              No Active Projects Found
            </p>
            <p className="text-sm font-bold uppercase tracking-wide text-neutral-400">
              Create a new project or connect in the yard to sync assigned jobs.
            </p>
          </div>
          <FatButton
            type="button"
            variant="primary"
            icon={Plus}
            onClick={handleOpenModal}
          >
            Create New Project
          </FatButton>
        </div>
      )}

      {!loading && projects.length > 0 && (
        <div className="grid grid-cols-1 gap-4" role="radiogroup" aria-label="Assigned Active Projects">
          {projects.map((project) => {
            const isSelected = selectedProject?.id === project.id;
            const clientLabel = project.client_name || project.clientName;

            return (
              <motion.button
                key={project.id}
                type="button"
                role="radio"
                aria-checked={isSelected}
                onClick={() => onSelectProject(project)}
                whileTap={{ scale: 0.98 }}
                className={`
                  w-full min-h-touch-lg p-5 rounded-2xl text-left
                  flex items-center justify-between gap-4
                  select-none touch-manipulation transition-colors
                  focus:outline-none focus:ring-4 focus:ring-primary
                  ${
                    isSelected
                      ? 'bg-primary text-black border-4 border-white shadow-hard'
                      : 'bg-dark-surface text-white border-4 border-dark-border hover:border-white shadow-hard-sm'
                  }
                `}
              >
                <div className="space-y-1 min-w-0">
                  {project.code && (
                    <span
                      className={`inline-block text-xs font-black uppercase tracking-widest px-2.5 py-1 rounded-md ${
                        isSelected
                          ? 'bg-black text-primary'
                          : 'bg-black text-primary border border-primary'
                      }`}
                    >
                      {project.code}
                    </span>
                  )}
                  <p className="text-xl sm:text-2xl font-black uppercase tracking-tight truncate">
                    {project.name || 'Unnamed Irrigation Project'}
                  </p>
                  {clientLabel && (
                    <p
                      className={`flex items-center gap-1.5 text-sm font-extrabold uppercase tracking-wider ${
                        isSelected ? 'text-neutral-900' : 'text-primary'
                      }`}
                    >
                      <User className="w-4 h-4 shrink-0 stroke-[2.5]" aria-hidden="true" />
                      <span className="truncate">{clientLabel}</span>
                    </p>
                  )}
                  {project.location && (
                    <p
                      className={`flex items-center gap-1.5 text-sm font-extrabold uppercase tracking-wider ${
                        isSelected ? 'text-neutral-900' : 'text-neutral-300'
                      }`}
                    >
                      <MapPin className="w-5 h-5 shrink-0 stroke-[2.5]" aria-hidden="true" />
                      <span className="truncate">{project.location}</span>
                    </p>
                  )}
                </div>

                <div
                  className={`
                    w-12 h-12 rounded-xl border-2 flex items-center justify-center shrink-0
                    ${
                      isSelected
                        ? 'bg-black text-primary border-black'
                        : 'bg-black text-neutral-500 border-dark-border'
                    }
                  `}
                >
                  <CheckCircle2 className="w-8 h-8 stroke-[3]" aria-hidden="true" />
                </div>
              </motion.button>
            );
          })}
        </div>
      )}

      {/* High-Contrast "New Project" Modal */}
      {isModalOpen && (
        <div
          role="dialog"
          aria-modal="true"
          aria-labelledby="new-project-modal-title"
          className="fixed inset-0 z-50 bg-black/85 flex items-center justify-center p-4"
        >
          <div className="w-full max-w-md bg-dark-surface border-4 border-white rounded-2xl p-6 space-y-5 shadow-hard">
            <div className="flex items-center justify-between border-b-4 border-dark-border pb-4">
              <div>
                <span className="text-xs font-black uppercase tracking-widest text-primary">
                  Create Job
                </span>
                <h3
                  id="new-project-modal-title"
                  className="text-2xl font-black uppercase tracking-tight text-white"
                >
                  New Project
                </h3>
              </div>

              <button
                type="button"
                onClick={handleCloseModal}
                disabled={creatingProject}
                aria-label="Close modal"
                className="w-12 h-12 rounded-xl bg-black text-white border-2 border-white flex items-center justify-center hover:bg-safety-red transition-colors"
              >
                <X className="w-6 h-6 stroke-[2.75]" aria-hidden="true" />
              </button>
            </div>

            {modalError && (
              <div
                role="alert"
                className="p-3 rounded-xl bg-safety-red text-white border-2 border-white flex items-center gap-2 font-extrabold text-sm uppercase"
              >
                <AlertCircle className="w-6 h-6 shrink-0 stroke-[2.5]" aria-hidden="true" />
                <span>{modalError}</span>
              </div>
            )}

            <form onSubmit={handleCreateProject} className="space-y-4">
              <div>
                <label
                  htmlFor="new-project-name"
                  className="block text-sm font-black uppercase tracking-wider text-white mb-2"
                >
                  1. Project Name
                </label>
                <input
                  id="new-project-name"
                  type="text"
                  required
                  value={newProjectName}
                  onChange={(e) => setNewProjectName(e.target.value.toUpperCase())}
                  placeholder="max 12 characters"
                  minLength={3}
                  maxLength={12}
                  className="
                    w-full min-h-touch px-4 py-3 rounded-xl
                    bg-black text-white text-lg font-bold
                    border-4 border-white
                    placeholder:text-neutral-500
                    focus:outline-none focus:border-primary focus:ring-4 focus:ring-primary
                  "
                />
              </div>

              <div>
                <label
                  htmlFor="new-project-client"
                  className="block text-sm font-black uppercase tracking-wider text-white mb-2"
                >
                  2. Client
                </label>
                <input
                  id="new-project-client"
                  type="text"
                  required
                  value={newClientName}
                  onChange={(e) => setNewClientName(e.target.value)}
                  placeholder="e.g. Gippsland Dairy Co"
                  className="
                    w-full min-h-touch px-4 py-3 rounded-xl
                    bg-black text-white text-lg font-bold
                    border-4 border-white
                    placeholder:text-neutral-500
                    focus:outline-none focus:border-primary focus:ring-4 focus:ring-primary
                  "
                />
              </div>

              <div className="pt-2 grid grid-cols-2 gap-3">
                <FatButton
                  type="button"
                  variant="secondary"
                  onClick={handleCloseModal}
                  disabled={creatingProject}
                >
                  Cancel
                </FatButton>
                <FatButton
                  type="submit"
                  variant="primary"
                  icon={Plus}
                  disabled={creatingProject}
                >
                  {creatingProject ? 'Saving...' : 'Create'}
                </FatButton>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}

export default Step1Project;

