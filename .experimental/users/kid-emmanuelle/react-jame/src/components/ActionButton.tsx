interface ActionButtonProps {
  variant: 'primary' | 'accent' | 'secondary' | 'neutral';
  onClick: () => void;
  label: string;
  fullWidth?: boolean;
  large?: boolean;
  disabled?: boolean;
  icon?: string;
}

export default function ActionButton({
  variant,
  onClick,
  label,
  fullWidth = false,
  large = false,
  disabled = false,
  icon,
}: ActionButtonProps) {
  const classes = [
    'action-button',
    `action-button--${variant}`,
    fullWidth ? 'action-button--full-width' : '',
    large ? 'action-button--large' : '',
    disabled ? 'opacity-50 cursor-not-allowed' : '',
  ]
    .filter(Boolean)
    .join(' ');

  return (
    <button className={classes} onClick={disabled ? undefined : onClick} disabled={disabled}>
      <div className="action-button__plus">+</div>
      <div className="action-button__content">
        {icon && <span className="action-button__icon">{icon}</span>}
        <span className="action-button__label">{label}</span>
      </div>
    </button>
  );
}
