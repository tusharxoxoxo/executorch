/*
 * Copyright (c) Meta Platforms, Inc. and affiliates.
 * All rights reserved.
 *
 * This source code is licensed under the BSD-style license found in the
 * LICENSE file in the root directory of this source tree.
 */

#include <executorch/kernels/portable/cpu/vec_ops.h>
#include <executorch/runtime/kernel/kernel_includes.h>
// Performs a batch matrix-matrix product of matrices stored in input and mat2.

// input and mat2 must be 3-D tensors each containing the same number of
// matrices.

// If input is a (b \times n \times m)(b×n×m) tensor, mat2 is a (b \times m
// \times p)(b×m×p) tensor, out will be a (b \times n \times p)(b×n×p) tensor.

// Note: This function does not broadcast. For broadcasting matrix products, see
// matmul().
namespace torch {
namespace executor {
namespace native {

using Tensor = exec_aten::Tensor;

namespace {

// Asserts that the parameters are valid.
void check_bmm_out_args(const Tensor& self, const Tensor& mat2, Tensor& out) {
  // Ensure dimensions is 3 for all input and out
  ET_CHECK_MSG(
      self.dim() == mat2.dim(),
      "self.dim() %zd != mat2.dim() %zd",
      self.dim(),
      mat2.dim());
  ET_CHECK_MSG(
      self.dim() == out.dim(),
      "self.dim() %zd != out.dim() %zd",
      self.dim(),
      out.dim());
  ET_CHECK_MSG(self.dim() == 3, "self.dim() %zd != 3", self.dim());
  // Ensure batch larger than or equals to 0
  ET_CHECK_MSG(self.size(0) >= 0, "self.size(0) %zd < 0", self.size(0));
  // Ensure batches are the same
  ET_CHECK_MSG(
      self.size(0) == mat2.size(0),
      "self.size(0) %zd != mat2.size(0) %zd",
      self.size(0),
      mat2.size(0));
  ET_CHECK_MSG(
      self.size(0) == out.size(0),
      "self.size(0) %zd != out.size(0) %zd",
      self.size(0),
      out.size(0));
  // Ensure the out size is compatible with input tensors
  ET_CHECK_MSG(
      mat2.size(2) == out.size(2),
      "mat2.size(2) %zd != out.size(2) %zd",
      mat2.size(2),
      out.size(2));
  ET_CHECK_MSG(
      self.size(1) == out.size(1),
      "self.size(1) %zd != out.size(1) %zd",
      self.size(1),
      out.size(1));
}

// This doesn't handle overflow yet
template <typename CTYPE>
void bmm_kernel(const Tensor& self, const Tensor& mat2, Tensor& out) {
  if (self.numel() == 0 || mat2.numel() == 0 || out.numel() == 0) {
    return;
  }
  const CTYPE* x_data = self.const_data_ptr<CTYPE>();
  const CTYPE* y_data = mat2.const_data_ptr<CTYPE>();
  CTYPE* z_data = out.mutable_data_ptr<CTYPE>();

  int64_t batch_size = self.size(0);
  int64_t m = self.size(1);
  int64_t n = self.size(2);
  int64_t p = mat2.size(2);

  for (int i = 0; i < batch_size; ++i) {
    const CTYPE* x = x_data + i * m * n;
    const CTYPE* y = y_data + i * n * p;
    CTYPE* z = z_data + i * m * p;

    vec_matmul<CTYPE>(z, x, y, m, n, p);
  }
}

void resize_out_tensor(const Tensor& self, const Tensor& mat2, Tensor& out) {
  exec_aten::SizesType expected_output_size[kTensorDimensionLimit];

  const size_t m_dim = self.dim() - 2;
  const size_t n_dim = self.dim() - 1;

  for (size_t i = 0; i < m_dim; i++) {
    expected_output_size[i] = self.size(i);
  }

  expected_output_size[m_dim] = self.size(m_dim);
  expected_output_size[n_dim] = mat2.size(n_dim);

  ArrayRef<exec_aten::SizesType> output_size{
      expected_output_size, static_cast<size_t>(out.dim())};

  torch::executor::Error err = resize_tensor(out, output_size);
  ET_CHECK_MSG(
      err == torch::executor::Error::Ok,
      "Failed to resize out Tensor in bmm_out");
}
} // namespace

// bmm.out(Tensor self, Tensor mat2, *, Tensor(a!) out) -> Tensor(a!)
Tensor& bmm_out(
    RuntimeContext& ctx,
    const Tensor& self,
    const Tensor& mat2,
    Tensor& out) {
  (void)ctx;
  resize_out_tensor(self, mat2, out);
  check_bmm_out_args(self, mat2, out);
  ET_CHECK_SAME_DTYPE3(self, mat2, out);
  auto scalar_type = self.scalar_type();
#define BMM_TENSOR(ctype, dtype)        \
  case ScalarType::dtype:               \
    bmm_kernel<ctype>(self, mat2, out); \
    break;

  switch (scalar_type) {
    ET_FORALL_REAL_TYPES(BMM_TENSOR)
    default:
      ET_CHECK_MSG(false, "Unhandled dtype %hhd", scalar_type);
  }
#undef BMM_TENSOR
  return out;
}

} // namespace native
} // namespace executor
} // namespace torch
