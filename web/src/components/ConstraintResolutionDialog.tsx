import { useState } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import { PauseCircle, X } from 'lucide-react';
import type { ConstraintInputAnswer, ConstraintInputChoice, PendingConstraintInputRequest } from '../types';
import { CONSTRAINT_RESOLUTION_COPY as copy } from '../lib/constraintResolutionCopy';

export interface ConstraintResolutionDialogProps {
  request: PendingConstraintInputRequest;
  open: boolean;
  canClose: boolean;
  resuming: boolean;
  errorMessage: string | null;
  onOpenChange: (open: boolean) => void;
  onSubmit: (answer: ConstraintInputAnswer) => void;
}

export function ConstraintResolutionDialog({
  request,
  open,
  canClose,
  resuming,
  errorMessage,
  onOpenChange,
  onSubmit,
}: ConstraintResolutionDialogProps) {
  const [choice, setChoice] = useState<ConstraintInputChoice | null>(null);
  const [customText, setCustomText] = useState('');

  const savedAnswer = request.answer;
  const selectedChoice = savedAnswer?.choice ?? choice;
  const submitDisabled = resuming || (
    savedAnswer === null && (
      selectedChoice === null ||
      (selectedChoice === 'custom' && !customText.trim())
    )
  );

  const submit = () => {
    if (savedAnswer) {
      onSubmit(savedAnswer);
      return;
    }
    if (!selectedChoice) return;
    const answer: ConstraintInputAnswer = {
      request_id: request.request_id,
      choice: selectedChoice,
      ...(selectedChoice === 'custom' ? { custom_text: customText.trim() } : {}),
    };
    onSubmit(answer);
  };

  return (
    <>
      <div className="pause-panel constraint-input-pending" role="status" aria-live="polite">
        <div className="pause-panel-head">
          <PauseCircle size={14} aria-hidden="true" />
          <span>{savedAnswer ? copy.answerSaved : copy.waitingForAnswer}</span>
        </div>
        <div className="pause-panel-facts">{request.question}</div>
        <div className="pause-panel-resume">
          <button
            className="pause-resume-btn"
            type="button"
            onClick={() => onOpenChange(true)}
            disabled={resuming}
          >
            {savedAnswer ? copy.continue : copy.openQuestion}
          </button>
          <span className="pause-panel-hint">{copy.sameRunHint}</span>
        </div>
      </div>

      <Dialog.Root open={open} onOpenChange={onOpenChange}>
        <Dialog.Portal>
          <Dialog.Overlay className="palette-overlay" />
          <Dialog.Content
            className="project-dialog constraint-input-dialog"
            aria-describedby="constraint-input-description"
          >
            <div className="project-dialog-head">
              <Dialog.Title className="project-dialog-title">{copy.title}</Dialog.Title>
              {canClose && (
                <Dialog.Close asChild>
                  <button className="icon-btn project-dialog-close" type="button" aria-label={copy.close}>
                    <X size={14} />
                  </button>
                </Dialog.Close>
              )}
            </div>
            <Dialog.Description id="constraint-input-description" className="project-dialog-desc">
              {request.question} {copy.description}
            </Dialog.Description>
            <div className="constraint-input-values">
              <section className="constraint-input-value" aria-labelledby="constraint-old-label">
                <h3 id="constraint-old-label">{copy.oldConstraint}</h3>
                <p>{request.old_value}</p>
              </section>
              <section className="constraint-input-value" aria-labelledby="constraint-new-label">
                <h3 id="constraint-new-label">{copy.newConstraint}</h3>
                <p>{request.candidate}</p>
              </section>
            </div>

            {savedAnswer ? (
              <div className="project-dialog-target" role="status">
                <strong>{copy.answerSaved}</strong>
                <span>{request.choices.find((item) => item.id === savedAnswer.choice)?.label ?? savedAnswer.choice}</span>
                {savedAnswer.custom_text && <p>{savedAnswer.custom_text}</p>}
              </div>
            ) : (
              <fieldset className="constraint-input-choices" disabled={resuming}>
                <legend>{copy.chooseResolution}</legend>
                {request.choices.map((item) => (
                  <label
                    className={`constraint-input-choice${choice === item.id ? ' selected' : ''}`}
                    key={item.id}
                  >
                    <input
                      type="radio"
                      name={`constraint-resolution-${request.request_id}`}
                      value={item.id}
                      checked={choice === item.id}
                      onChange={() => setChoice(item.id)}
                    />
                    <span>{item.label}</span>
                  </label>
                ))}
                {choice === 'custom' && (
                  <label className="project-field constraint-input-custom">
                    <span className="project-field-label">{copy.customLabel}</span>
                    <textarea
                      className="project-input"
                      value={customText}
                      onChange={(event) => setCustomText(event.target.value)}
                      maxLength={100_000}
                      rows={4}
                      autoFocus
                      placeholder={copy.customPlaceholder}
                    />
                  </label>
                )}
              </fieldset>
            )}

            {errorMessage && <div className="project-error" role="alert">{errorMessage}</div>}

            <div className="project-dialog-actions">
              {canClose && (
                <Dialog.Close asChild>
                  <button className="project-btn" type="button">{copy.close}</button>
                </Dialog.Close>
              )}
              <button
                className="project-btn project-btn-primary"
                type="button"
                onClick={submit}
                disabled={submitDisabled}
              >
                {resuming ? copy.resuming : savedAnswer ? copy.continue : copy.submit}
              </button>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </>
  );
}
