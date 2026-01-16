import React from 'react';
import { Exercise, QuestionProps } from './types';
import { SingleChoice } from './QuestionTypes/SingleChoice';
import { MultiChoice } from './QuestionTypes/MultiChoice';
import { TrueFalse } from './QuestionTypes/TrueFalse';
import { FillGaps } from './QuestionTypes/FillGaps';
import { Rearrange } from './QuestionTypes/Rearrange';

interface Props {
    exercise: Exercise;
    value: any;
    onChange: (val: any) => void;
    submitted: boolean;
    seed: number;
}

export const ExerciseRenderer: React.FC<Props> = (props) => {
    const { exercise } = props;

    switch (exercise.question_type) {
        case 'single_choice':
            return <SingleChoice {...props} exercise={exercise} />;
        case 'multi_choice':
            return <MultiChoice {...props} exercise={exercise} />;
        case 'true_false':
            return <TrueFalse {...props} exercise={exercise} />;
        case 'fill_gaps':
            return <FillGaps {...props} exercise={exercise} />;
        case 'rearrange':
            return <Rearrange {...props} exercise={exercise} />;
        default:
            return <div>Unsupported question type: {(exercise as any).question_type}</div>;
    }
};
