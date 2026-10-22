import React, { useState } from 'react';
import { Navigate, useNavigate } from 'react-router-dom';
import { LogIn, AlertTriangle, Droplets } from 'lucide-react';
import { useAuth } from '../context/AuthContext';
import { FatButton } from '../components/ui/FatButton';

export function Login() {
  const { currentUser, loginWithEmail } = useAuth();
  const navigate = useNavigate();

  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);

  // If already logged in, redirect straight to the dashboard
  if (currentUser) {
    return <Navigate to="/" replace />;
  }

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError('');

    if (!email.trim() || !password) {
      setError('Enter both operator email and 6-digit PIN.');
      return;
    }

    if (!/^\d{6}$/.test(password)) {
      setError('PIN must be exactly 6 digits (0-9).');
      return;
    }

    try {
      setSubmitting(true);
      await loginWithEmail(email.trim(), password);
      navigate('/', { replace: true });
    } catch (err) {
      console.error('[Login Error]', err);
      setError('Invalid operator email or 6-digit PIN.');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="min-h-screen bg-dark text-white flex flex-col justify-center px-4 py-8 sm:px-6">
      <div className="w-full max-w-md mx-auto bg-dark-surface border-4 border-white rounded-2xl p-6 sm:p-8 shadow-hard">
        {/* Brand Header */}
        <div className="flex items-center gap-4 border-b-4 border-dark-border pb-6 mb-6">
          <div className="w-16 h-16 rounded-xl bg-primary border-2 border-black flex items-center justify-center shrink-0">
            <Droplets className="w-10 h-10 text-black stroke-[2.75]" aria-hidden="true" />
          </div>
          <div>
            <p className="text-xs font-black uppercase tracking-widest text-primary">
              LaTrobe Irrigation
            </p>
            <h1 className="text-3xl font-black uppercase tracking-tight text-white">
              Sign In
            </h1>
          </div>
        </div>

        {/* High-Visibility Error Banner */}
        {error && (
          <div
            role="alert"
            className="mb-6 p-4 rounded-xl bg-safety-red text-white border-2 border-white flex items-center gap-3 font-extrabold text-base uppercase tracking-wide"
          >
            <AlertTriangle className="w-8 h-8 shrink-0 stroke-[2.75]" aria-hidden="true" />
            <span>{error}</span>
          </div>
        )}

        {/* Login Form */}
        <form onSubmit={handleSubmit} className="space-y-6" noValidate>
          <div>
            <label
              htmlFor="operator-email"
              className="block text-base font-black uppercase tracking-wider text-white mb-2"
            >
              Email
            </label>
            <input
              id="operator-email"
              type="email"
              autoComplete="email"
              inputMode="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="operator@latrobe.com.au"
              className="
                w-full min-h-touch-lg px-5 py-4 rounded-xl
                bg-black text-white text-xl font-bold
                border-4 border-white
                placeholder:text-neutral-500
                focus:outline-none focus:border-primary focus:ring-4 focus:ring-primary
              "
            />
          </div>

          <div>
            <label
              htmlFor="operator-pin"
              className="block text-base font-black uppercase tracking-wider text-white mb-2"
            >
              6-Digit PIN
            </label>
            <input
              id="operator-pin"
              type="password"
              inputMode="numeric"
              pattern="[0-9]*"
              maxLength={6}
              autoComplete="current-password"
              required
              value={password}
              onChange={(e) => {
                const digitsOnly = e.target.value.replace(/\D/g, '').slice(0, 6);
                setPassword(digitsOnly);
              }}
              placeholder="••••••"
              className="
                w-full min-h-touch-lg px-5 py-4 rounded-xl
                bg-black text-white text-2xl font-black tracking-[0.5em]
                border-4 border-white
                placeholder:text-neutral-500 placeholder:tracking-[0.5em]
                focus:outline-none focus:border-primary focus:ring-4 focus:ring-primary
              "
            />
          </div>

          <div className="pt-2">
            <FatButton
              type="submit"
              variant="primary"
              icon={LogIn}
              disabled={submitting}
              className="min-h-touch-lg text-2xl"
            >
              {submitting ? 'Signing In...' : 'Sign In'}
            </FatButton>
          </div>
        </form>
      </div>
    </div>
  );
}

export default Login;
