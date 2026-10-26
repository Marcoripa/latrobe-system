import React, { useState } from 'react';
import { ArrowLeft, ArrowRight } from 'lucide-react';
import { FatButton } from '../../components/ui/FatButton';
import { Step1Project } from './Step1Project';
import { Step2Time } from './Step2Time';
import { Step3Materials } from './Step3Materials';
import { Step4Evidence } from './Step4Evidence';
import { Step5Review } from './Step5Review';

const TOTAL_STEPS = 5;

/**
 * WizardFlow - Complete 5-step offline-first Shift Logging Wizard:
 * Step 1: Project Selection
 * Step 2: Log Time (Stepper + Category QuickChips)
 * Step 3: Log Materials (QuickChips + Per-item Steppers)
 * Step 4: Snap & Speak (Camera + SpeechRecognition)
 * Step 5: Review & Swipe to Log (Firestore offline-capable submission)
 */
export function WizardFlow() {
  const [currentStep, setCurrentStep] = useState(1);

  // Complete Wizard State
  const [selectedProject, setSelectedProject] = useState(null);
  const [hoursByCategory, setHoursByCategory] = useState({
    Travel: 0,
    'On-Site': 8,
    'Shed/Prep': 0,
  });
  const [itemsUsed, setItemsUsed] = useState([]);
  const [photo, setPhoto] = useState(null);
  const [notes, setNotes] = useState('');

  const canProceedFromStep1 = Boolean(selectedProject);
  const canProceedFromStep2 =
    Object.values(hoursByCategory).reduce((sum, h) => sum + Number(h || 0), 0) >
    0;

  const handleNext = () => {
    if (currentStep < TOTAL_STEPS) {
      setCurrentStep((prev) => prev + 1);
      window.scrollTo({ top: 0, behavior: 'smooth' });
    }
  };

  const handleBack = () => {
    if (currentStep > 1) {
      setCurrentStep((prev) => prev - 1);
      window.scrollTo({ top: 0, behavior: 'smooth' });
    }
  };

  const handleResetWizard = () => {
    setSelectedProject(null);
    setHoursByCategory({ Travel: 0, 'On-Site': 8, 'Shed/Prep': 0 });
    setItemsUsed([]);
    setPhoto(null);
    setNotes('');
    setCurrentStep(1);
    window.scrollTo({ top: 0, behavior: 'smooth' });
  };

  return (
    <div className="flex flex-col min-h-[calc(100vh-120px)] justify-between gap-6">
      {/* High-Contrast 5-Step Progress Bar */}
      <div className="space-y-3">
        <div className="grid grid-cols-5 gap-2" aria-label="Wizard Progress">
          {[1, 2, 3, 4, 5].map((stepNumber) => {
            const isActive = stepNumber === currentStep;
            const isCompleted = stepNumber < currentStep;
            return (
              <div
                key={stepNumber}
                className={`h-4 rounded-lg border-2 transition-colors ${
                  isActive
                    ? 'bg-primary border-white'
                    : isCompleted
                    ? 'bg-safety-green border-black'
                    : 'bg-dark-surface border-dark-border'
                }`}
              />
            );
          })}
        </div>

        {/* Selected Project Context Pill on Steps 2-5 */}
        {selectedProject && currentStep > 1 && (
          <div className="px-4 py-2.5 rounded-xl bg-black border-2 border-primary flex items-center justify-between">
            <span className="text-xs font-black uppercase tracking-widest text-primary">
              Project
            </span>
            <span className="text-base font-black uppercase text-white truncate ml-3">
              {selectedProject.name}
            </span>
          </div>
        )}
      </div>

      {/* Step Content */}
      <div className="flex-1 py-2">
        {currentStep === 1 && (
          <Step1Project
            selectedProject={selectedProject}
            onSelectProject={setSelectedProject}
          />
        )}

        {currentStep === 2 && (
          <Step2Time
            hoursByCategory={hoursByCategory}
            onChangeHoursByCategory={setHoursByCategory}
          />
        )}

        {currentStep === 3 && (
          <Step3Materials
            itemsUsed={itemsUsed}
            onChangeItemsUsed={setItemsUsed}
          />
        )}

        {currentStep === 4 && (
          <Step4Evidence
            photo={photo}
            onChangePhoto={setPhoto}
            notes={notes}
            onChangeNotes={setNotes}
          />
        )}

        {currentStep === 5 && (
          <Step5Review
            selectedProject={selectedProject}
            hoursByCategory={hoursByCategory}
            itemsUsed={itemsUsed}
            photo={photo}
            notes={notes}
            onReset={handleResetWizard}
          />
        )}
      </div>

      {/* Bottom Fat-Finger Navigation Bar */}
      <footer className="pt-4 border-t-4 border-dark-border grid grid-cols-2 gap-4">
        {currentStep > 1 ? (
          <FatButton
            variant="secondary"
            icon={ArrowLeft}
            onClick={handleBack}
            className={currentStep === TOTAL_STEPS ? 'col-span-2' : ''}
          >
            Back
          </FatButton>
        ) : (
          <div />
        )}

        {currentStep < TOTAL_STEPS && (
          <FatButton
            variant="primary"
            icon={ArrowRight}
            onClick={handleNext}
            disabled={
              (currentStep === 1 && !canProceedFromStep1) ||
              (currentStep === 2 && !canProceedFromStep2)
            }
          >
            Next
          </FatButton>
        )}
      </footer>
    </div>
  );
}

export default WizardFlow;
