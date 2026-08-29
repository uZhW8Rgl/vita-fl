export const isExplicitlyFinalizedSourceRound = ({
    published,
    completed,
}: {
    published: boolean;
    completed: boolean;
}) => published === true && completed === true;
