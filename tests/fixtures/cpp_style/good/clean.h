// Copyright (c) 2026 Celestia Island. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

#pragma once

#include <cstdint>

namespace sample
{

    class widget
    {
        public:
            void update();

        protected:
            std::uint32_t value = 0;
    };

}
