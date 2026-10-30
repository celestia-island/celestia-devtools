// Copyright (c) 2026 Celestia Island. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

#include <stddef.h>

int compute_value(int value)
{
    if(value > 0)
    {
        return value;
    }

    for(int index = 0;index < 3;index++)
    {
        value = value + index;
    }

    // plain C, no C++-only sugar needed for the lexical rules.
    return value;
}
